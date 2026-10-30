#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
flasher.py — 一键刷机 (flash.sh 的 Python 版)

扫描待刷包目录, 按 ChipMode 自动匹配设备, 刷 xp5_a/xp5_b/vlmu/mcu:
  大包 -> <pkg>/flash_tools/adc_flashall.sh <serial>   (脚本自带 fastboot 流程)
  小包 -> <pkg>/deploy_xpilot_by_adb.sh <xp5_vlmu|xp5_a|xp5_b>
  mcu  -> mcu.flash_mcu (adb A/B 双区, 从解压目录取 *_APPA.hex/*_APPB.hex)

包分类按文件名前缀 (大小写敏感), 规则收敛在 common.classify:
  VLMU*=vlmu大包  vlmu*=vlmu小包  XCCP*=xp5大包  xpilot*=xp5小包  XPilot_AURIX*=mcu

既可命令行使用 (与 flash.sh 参数兼容), 也可被 flash_gui.py import:
  CLI:  python3 flasher.py -n all
        python3 flasher.py -d <dir> vlmu
  import:
        import flasher
        flasher.run_flash("<dir>", ["vlmu"], dry_run=False, log=print)
"""

import os
import sys
import tarfile
import zipfile
import argparse

import common
import mcu

# target -> (大包类别, 小包类别)
TARGET_CATS = {
    "vlmu":  ("vlmu_big", "vlmu_small"),
    "xp5_a": ("xp5_big", "xp5_small"),
    "xp5_b": ("xp5_big", "xp5_small"),
}
# target -> ChipMode (mcu 走 SoC_A 设备)
TARGET_MODE = {"vlmu": "SoC_VLMU", "xp5_a": "SoC_A",
               "xp5_b": "SoC_B", "mcu": "SoC_A"}
# 小包 deploy_xpilot_by_adb.sh 的类型参数
SMALL_TYPE = {"vlmu": "xp5_vlmu", "xp5_a": "xp5_a", "xp5_b": "xp5_b"}

ALL_ORDER = ("vlmu", "xp5_a", "xp5_b", "mcu")


# ---------------------------------------------------------------- 解压
def _work_dir_for(pkg, work_root):
    b = os.path.basename(pkg)
    for ext in (".zip", ".tgz", ".tar.gz", ".tar"):
        if b.endswith(ext):
            b = b[: -len(ext)]
            break
    return os.path.join(work_root, b)


def _unzip_keep_perms(zip_path, dest):
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            target = zf.extract(info, dest)
            mode = (info.external_attr >> 16) & 0xFFFF
            if mode:
                try:
                    os.chmod(target, mode)
                except OSError:
                    pass


def extract_pkg(pkg, dest, log):
    if os.path.isdir(dest) and os.listdir(dest):
        log(f"[INFO] already extracted, reuse: {dest}")
        return True
    
    # 先验证压缩包完整性
    if pkg.endswith((".tar.gz", ".tgz")):
        log(f"[INFO] 验证压缩包完整性: {os.path.basename(pkg)}")
        try:
            import gzip
            # 尝试打开并读取 tar 文件头, 检查是否损坏
            with tarfile.open(pkg, "r:gz") as tf:
                members = tf.getmembers()
                if not members:
                    log(f"[ERROR] 压缩包为空: {os.path.basename(pkg)}")
                    return False
                log(f"[INFO] 压缩包包含 {len(members)} 个文件")
        except tarfile.TarError as e:
            log(f"[ERROR] 压缩包损坏 (tar 格式错误): {os.path.basename(pkg)}")
            log(f"[ERROR] 错误详情: {e}")
            log(f"[ERROR] 请删除文件后重新下载: {pkg}")
            return False
        except gzip.BadGzipFile as e:
            log(f"[ERROR] 压缩包损坏 (gzip 格式错误): {os.path.basename(pkg)}")
            log(f"[ERROR] 请删除文件后重新下载: {pkg}")
            return False
        except EOFError as e:
            log(f"[ERROR] 压缩包不完整 (unexpected EOF): {os.path.basename(pkg)}")
            log(f"[ERROR] 可能下载中断, 请删除后重新下载: {pkg}")
            return False
        except Exception as e:
            log(f"[WARN] 验证压缩包时出错: {e}")
            # 继续尝试解压, 让后续步骤报错
    
    os.makedirs(dest, exist_ok=True)
    log(f"[OK] ==> Extracting {os.path.basename(pkg)}")
    try:
        if pkg.endswith((".tar.gz", ".tgz", ".tar")):
            with tarfile.open(pkg) as tf:
                try:
                    tf.extractall(dest, filter="tar")   # Py3.12+: 允许符号链接
                except TypeError:
                    tf.extractall(dest)                 # Py<3.12: 无 filter 参数
        elif pkg.endswith(".zip"):
            _unzip_keep_perms(pkg, dest)
        else:
            log(f"[ERROR] unknown archive type: {pkg}")
            return False
    except (tarfile.TarError, OSError) as e:
        log(f"[ERROR] 解压失败 {os.path.basename(pkg)}: {e}")
        log(f"[ERROR] 压缩包可能损坏, 请删除后重新下载: {pkg}")
        return False
    except Exception as e:  # noqa
        log(f"[ERROR] 解压失败 {os.path.basename(pkg)}: {e}")
        return False
    return True


# ---------------------------------------------------------------- 查找
def _find_file(root, name, path_contains=None):
    for dirpath, _, files in os.walk(root):
        if name in files:
            full = os.path.join(dirpath, name)
            if path_contains is None or path_contains in full:
                return full
    return None


def _find_file_with_prefix(root, prefix, path_contains=None):
    """查找文件名以 prefix 开头的文件。"""
    for dirpath, _, files in os.walk(root):
        for f in files:
            if f.startswith(prefix):
                full = os.path.join(dirpath, f)
                if path_contains is None or path_contains in full:
                    return full
    return None


def _find_suffix(root, suffix):
    for dirpath, _, files in os.walk(root):
        for f in files:
            if f.endswith(suffix):
                return os.path.join(dirpath, f)
    return None


def _chmod_x(path):
    try:
        os.chmod(path, os.stat(path).st_mode | 0o111)
    except OSError:
        pass


# ---------------------------------------------------------------- 设备
def build_serial_map(devices):
    m = {}
    for s, mode in devices:
        m.setdefault(mode, s)
    return m


def serial_for_target(target, serial_map, force_serial=""):
    if force_serial:
        return force_serial
    return serial_map.get(TARGET_MODE.get(target, ""), "")


# ---------------------------------------------------------------- 刷机例程
def flash_big(target, pkg, serial, work_root, log, dry_run):
    work = _work_dir_for(pkg, work_root)
    if dry_run:
        log(f"[DRY-RUN] extract {pkg}")
        log(f"[DRY-RUN] cd <pkg>/flash_tools && ./adc_flashall.sh {serial}")
        log("[INFO] adc_flashall.sh 自行进入 fastboot (auto_fastboot_by_usb_id.sh)")
        return True
    if not extract_pkg(pkg, work, log):
        return False
    flashall = _find_file(work, "adc_flashall.sh", path_contains="flash_tools")
    if not flashall:
        log(f"[ERROR] flash_tools/adc_flashall.sh not found in {pkg}")
        return False
    _chmod_x(flashall)
    log(f"[INFO] Flash BIG {target} via adc_flashall.sh (serial={serial})")
    rc = common.run_streaming(
        ["./adc_flashall.sh", serial], cwd=os.path.dirname(flashall), log=log)
    if rc != 0:
        log(f"[ERROR] FAILED (rc={rc}): adc_flashall.sh {target}")
        return False
    return True


def flash_small(target, pkg, work_root, log, dry_run):
    stype = SMALL_TYPE.get(target, target)
    work = _work_dir_for(pkg, work_root)
    if dry_run:
        log(f"[DRY-RUN] extract {pkg}")
        log(f"[DRY-RUN] cd <pkg> && ./deploy_xpilot_by_adb.sh {stype}")
        return True
    if not extract_pkg(pkg, work, log):
        return False
    deploy = _find_file(work, "deploy_xpilot_by_adb.sh")
    if not deploy:
        log(f"[ERROR] deploy_xpilot_by_adb.sh not found in {pkg}")
        return False
    _chmod_x(deploy)
    log(f"[INFO] Flash SMALL {target} via deploy_xpilot_by_adb.sh {stype}")
    rc = common.run_streaming(
        ["./deploy_xpilot_by_adb.sh", stype], cwd=os.path.dirname(deploy), log=log)
    if rc != 0:
        log(f"[ERROR] FAILED (rc={rc}): deploy_xpilot_by_adb.sh {target}")
        return False
    return True


def flash_mcu(pkg, serial, work_root, log, dry_run):
    work = _work_dir_for(pkg, work_root)
    if dry_run:
        log(f"[DRY-RUN] extract {pkg}")
        log("[DRY-RUN] find *_APPA.hex / *_APPB.hex in extracted dir")
        log(f"[DRY-RUN] mcu.flash_mcu(serial={serial}) via adb (A/B 双区)")
        return True
    if not extract_pkg(pkg, work, log):
        return False
    appa = _find_suffix(work, "_APPA.hex")
    appb = _find_suffix(work, "_APPB.hex")
    if not appa or not appb:
        log(f"[ERROR] *_APPA.hex / *_APPB.hex not found in {pkg}")
        return False
    log(f"[INFO] Flash MCU via mcu.flash_mcu (serial={serial})")
    return mcu.flash_mcu(appa, appb, serial=serial, slave=False, log=log)


# ---------------------------------------------------------------- 已解压目录刷机
def flash_big_extracted(target, extracted_dir, serial, log, dry_run):
    """大包刷机, 使用已解压目录。"""
    if dry_run:
        log(f"[DRY-RUN] cd {extracted_dir}/flash_tools && ./adc_flashall.sh {serial}")
        return True
    flashall = _find_file(extracted_dir, "adc_flashall.sh", path_contains="flash_tools")
    if not flashall:
        log(f"[ERROR] flash_tools/adc_flashall.sh not found in {extracted_dir}")
        return False
    _chmod_x(flashall)
    log(f"[INFO] Flash BIG {target} via adc_flashall.sh (serial={serial})")
    rc = common.run_streaming(
        ["./adc_flashall.sh", serial], cwd=os.path.dirname(flashall), log=log)
    if rc != 0:
        log(f"[ERROR] FAILED (rc={rc}): adc_flashall.sh {target}")
        return False
    return True


def flash_small_extracted(target, extracted_dir, log, dry_run):
    """小包刷机, 使用已解压目录。"""
    stype = SMALL_TYPE.get(target, target)
    if dry_run:
        log(f"[DRY-RUN] cd {extracted_dir} && ./deploy_xpilot_by_adb.sh {stype}")
        return True
    deploy = _find_file(extracted_dir, "deploy_xpilot_by_adb.sh")
    if not deploy:
        log(f"[ERROR] deploy_xpilot_by_adb.sh not found in {extracted_dir}")
        return False
    _chmod_x(deploy)
    log(f"[INFO] Flash SMALL {target} via deploy_xpilot_by_adb.sh {stype}")
    rc = common.run_streaming(
        ["./deploy_xpilot_by_adb.sh", stype], cwd=os.path.dirname(deploy), log=log)
    if rc != 0:
        log(f"[ERROR] FAILED (rc={rc}): deploy_xpilot_by_adb.sh {target}")
        return False
    return True


def flash_mcu_extracted(extracted_dir, serial, log, dry_run):
    """MCU 刷机, 使用已解压目录。"""
    if dry_run:
        log(f"[DRY-RUN] find *_APPA.hex / *_APPB.hex in {extracted_dir}")
        log(f"[DRY-RUN] mcu.flash_mcu(serial={serial}) via adb (A/B 双区)")
        return True
    appa = _find_suffix(extracted_dir, "_APPA.hex")
    appb = _find_suffix(extracted_dir, "_APPB.hex")
    if not appa or not appb:
        log(f"[ERROR] *_APPA.hex / *_APPB.hex not found in {extracted_dir}")
        return False
    log(f"[INFO] Flash MCU via mcu.flash_mcu (serial={serial})")
    return mcu.flash_mcu(appa, appb, serial=serial, slave=False, log=log)


# ---------------------------------------------------------------- VLMU MCU 刷机
def flash_vlmu_mcu(pkg, serial, work_root, log, dry_run):
    """VLMU MCU 刷机: 解压外层包, 找到内层 OTA 包, 推送到设备并执行升级。
    
    如果 pkg 本身就是 VLMUMCU_*.zip, 直接推送升级, 不解压。
    解压到包所在目录 (不放到 .extracted)。
    """
    pkg_basename = os.path.basename(pkg)
    
    # 检查是否直接是 VLMUMCU_*.zip
    if pkg_basename.startswith("VLMUMCU") and pkg_basename.endswith(".zip"):
        log(f"[INFO] 直接使用 VLMU MCU OTA 包: {pkg_basename}")
        if dry_run:
            log(f"[DRY-RUN] adb push {pkg_basename} to /tmp/ on VLMU (serial={serial})")
            log("[DRY-RUN] execute upgrade commands on VLMU")
            return True
        return _do_flash_vlmu_mcu_direct(pkg, serial, log)
    
    # 否则解压到包所在目录
    pkg_dir = os.path.dirname(os.path.abspath(pkg))
    work = _work_dir_for(pkg, pkg_dir)  # 解压到包所在目录, 不是 .extracted
    if dry_run:
        log(f"[DRY-RUN] extract {pkg} -> {work}")
        log(f"[DRY-RUN] find OTAPackage/VLMUMCU_*.zip in extracted dir")
        log(f"[DRY-RUN] adb push to /tmp/ on VLMU (serial={serial})")
        log("[DRY-RUN] execute upgrade commands on VLMU")
        return True
    if not extract_pkg(pkg, work, log):
        return False
    return _do_flash_vlmu_mcu(work, serial, log)


def flash_vlmu_mcu_extracted(extracted_dir, serial, log, dry_run):
    """VLMU MCU 刷机, 使用已解压目录。"""
    if dry_run:
        log(f"[DRY-RUN] find OTAPackage/VLMUMCU_*.zip in {extracted_dir}")
        log(f"[DRY-RUN] adb push to /tmp/ on VLMU (serial={serial})")
        log("[DRY-RUN] execute upgrade commands on VLMU")
        return True
    return _do_flash_vlmu_mcu(extracted_dir, serial, log)


def _do_flash_vlmu_mcu_direct(ota_pkg, serial, log):
    """直接推送 VLMUMCU_*.zip 并升级。"""
    ota_filename = os.path.basename(ota_pkg)
    log(f"[INFO] VLMU MCU OTA package: {ota_filename}")
    
    # 推送 OTA 包到设备 /tmp
    log(f"[INFO] Pushing {ota_filename} to VLMU /tmp/ ...")
    rc = common.run_streaming(
        ["adb", "-s", serial, "push", ota_pkg, f"/tmp/{ota_filename}"],
        log=log
    )
    if rc != 0:
        log(f"[ERROR] Failed to push OTA package to device (rc={rc})")
        return False
    
    return _run_vlmu_mcu_upgrade(ota_filename, serial, log)


def _do_flash_vlmu_mcu(extracted_dir, serial, log):
    """执行 VLMU MCU 升级的实际逻辑。"""
    # 查找内层 OTA 包 (OTAPackage/VLMUMCU_*.zip)
    ota_pkg = _find_file_with_prefix(extracted_dir, "VLMUMCU_", path_contains="OTAPackage")
    if not ota_pkg:
        # 尝试直接查找 VLMUMCU_*.zip
        ota_pkg = _find_file_with_prefix(extracted_dir, "VLMUMCU_")
        if not ota_pkg:
            log(f"[ERROR] VLMUMCU_*.zip not found in {extracted_dir}")
            return False
    
    ota_filename = os.path.basename(ota_pkg)
    log(f"[INFO] Found VLMU MCU OTA package: {ota_filename}")
    
    # 推送 OTA 包到设备 /tmp
    log(f"[INFO] Pushing {ota_filename} to VLMU /tmp/ ...")
    rc = common.run_streaming(
        ["adb", "-s", serial, "push", ota_pkg, f"/tmp/{ota_filename}"],
        log=log
    )
    if rc != 0:
        log(f"[ERROR] Failed to push OTA package to device (rc={rc})")
        return False
    
    return _run_vlmu_mcu_upgrade(ota_filename, serial, log)


def _run_vlmu_mcu_upgrade(ota_filename, serial, log):
    """在 VLMU 设备上执行升级命令。"""
    import time
    
    # Step 1: killall -9 update_service
    log("[INFO] Step 1: killall -9 update_service")
    rc = common.run_streaming(
        ["adb", "-s", serial, "shell", "killall -9 update_service"],
        log=log
    )
    log(f"[INFO] killall returned: {rc}")
    
    # Step 2: 创建启动脚本并推送到设备
    log("[INFO] Step 2: Creating startup script on device...")
    script_content = f"""#!/bin/sh
source /xpilot/xlauncher/scripts/env.bash
/xpilot/update/update_service -R -i /tmp/{ota_filename} > /dev/null 2>&1
"""
    # 写脚本到本地临时文件
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as f:
        f.write(script_content)
        local_script = f.name
    
    try:
        # 推送脚本到设备
        rc = common.run_streaming(
            ["adb", "-s", serial, "push", local_script, "/tmp/start_vlmu_mcu.sh"],
            log=log
        )
        if rc != 0:
            log("[ERROR] Failed to push startup script")
            return False
        
        # 设置执行权限
        common.run_streaming(
            ["adb", "-s", serial, "shell", "chmod +x /tmp/start_vlmu_mcu.sh"],
            log=log
        )
        
        # 清空旧日志, 方便监控新日志
        log("[INFO] Clearing old log file...")
        common.run_streaming(
            ["adb", "-s", serial, "shell", "> /xdata/log/latest/update_service.txt.log"],
            log=log
        )
        
        # 在前台执行脚本, 但输出重定向到 /dev/null
        # 我们通过读取日志文件来监控进度
        log("[INFO] Step 3: Running update_service (monitoring log file)...")
        
        # 启动一个后台线程来执行脚本
        import subprocess
        proc = subprocess.Popen(
            ["adb", "-s", serial, "shell", "/tmp/start_vlmu_mcu.sh"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL
        )
        
        # 监控日志文件
        log("[INFO] Step 4: Monitoring upgrade progress from log file...")
        last_line_count = 0
        upgrade_completed = False
        
        for i in range(600):  # 最多等待 10 分钟
            time.sleep(1)
            
            # 检查进程是否还在运行
            if proc.poll() is not None:
                log(f"[INFO] update_service process exited with code: {proc.returncode}")
                # 进程退出了, 再读一次日志确认结果
                time.sleep(1)
            
            # 每 5 秒读取一次日志
            if i % 5 == 0:
                log_output = []
                common.run_streaming(
                    ["adb", "-s", serial, "shell", "tail -20 /xdata/log/latest/update_service.txt.log"],
                    log=lambda x: log_output.append(x)
                )
                
                # 检查进度
                progress_output = []
                common.run_streaming(
                    ["adb", "-s", serial, "shell", "grep 'Current upgrade progress' /xdata/log/latest/update_service.txt.log | tail -1"],
                    log=lambda x: progress_output.append(x)
                )
                
                if progress_output:
                    progress_line = progress_output[0].strip()
                    log(f"[INFO] {i}s: {progress_line}")
                
                # 检查是否完成
                check_output = []
                common.run_streaming(
                    ["adb", "-s", serial, "shell", "grep -E 'Upgrade progress 100%|FlashExit|SwitchReboot' /xdata/log/latest/update_service.txt.log | tail -3"],
                    log=lambda x: check_output.append(x)
                )
                
                for line in check_output:
                    if "Upgrade progress 100%" in line:
                        log(f"[INFO] {i}s: {line.strip()}")
                    if "SwitchReboot" in line and "failed" not in line:
                        log(f"[INFO] {i}s: {line.strip()}")
                
                # 检查是否成功完成
                success_output = []
                common.run_streaming(
                    ["adb", "-s", serial, "shell", "grep -E 'Upgrade progress 100%.*Success|FlashExit.*Success' /xdata/log/latest/update_service.txt.log | tail -1"],
                    log=lambda x: success_output.append(x)
                )
                
                if success_output:
                    log(f"[OK] Upgrade completed successfully at {i}s")
                    upgrade_completed = True
                    break
                
                # 检查是否失败
                fail_output = []
                common.run_streaming(
                    ["adb", "-s", serial, "shell", "grep 'Fail!!!' /xdata/log/latest/update_service.txt.log | tail -1"],
                    log=lambda x: fail_output.append(x)
                )
                
                if fail_output:
                    log(f"[ERROR] Upgrade failed at {i}s: {fail_output[0].strip()}")
                    break
                
                # 如果进程退出了且没有明确的成功/失败标志
                if proc.poll() is not None and i > 30:
                    log(f"[WARN] Process exited but no clear success/fail flag found")
                    break
    finally:
        # 清理本地临时文件
        try:
            os.unlink(local_script)
        except:
            pass
    
    # Step 5: 检查升级是否成功
    log("[INFO] Step 5: Checking upgrade result...")
    success_output = []
    common.run_streaming(
        ["adb", "-s", serial, "shell", "grep -E 'Upgrade progress 100%|FlashExit|SwitchReboot' /xdata/log/latest/update_service.txt.log | tail -5"],
        log=lambda x: success_output.append(x)
    )
    
    # 检查是否有成功标志
    success = False
    for line in success_output:
        if "Upgrade progress 100%" in line or ("FlashExit" in line and "failed" not in line):
            success = True
            break
    
    # Step 6: 显示最终日志
    log("[INFO] Step 6: Final upgrade log (last 30 lines):")
    common.run_streaming(
        ["adb", "-s", serial, "shell", "tail -30 /xdata/log/latest/update_service.txt.log"],
        log=log
    )
    
    if success:
        log("[OK] VLMU MCU upgrade completed successfully.")
        return True
    else:
        log("[ERROR] VLMU MCU upgrade failed or did not complete.")
        return False


def _run_job(kind, target, pkg, serial, work_root, log, dry_run):
    log(f"[OK] ==> Target: {target}  [{kind}]")
    log(f"[INFO]   package : {os.path.basename(pkg)}")
    log(f"[INFO]   device  : {serial or '<none detected>'}")
    if not dry_run and not serial:
        log(f"[WARN]   no matching device online for '{target}' — skipped")
        return True
    if kind == "big":
        return flash_big(target, pkg, serial, work_root, log, dry_run)
    if kind == "small":
        return flash_small(target, pkg, work_root, log, dry_run)
    if kind == "mcu":
        return flash_mcu(pkg, serial, work_root, log, dry_run)
    if kind == "vlmu_mcu":
        return flash_vlmu_mcu(pkg, serial, work_root, log, dry_run)
    log(f"[ERROR]   unknown job kind: {kind}")
    return False


def _run_job_extracted(kind, target, extracted_dir, serial, work_root, log, dry_run):
    """处理已解压目录的刷机任务, 跳过解压步骤。"""
    log(f"[OK] ==> Target: {target}  [{kind}]")
    log(f"[INFO]   extracted dir : {os.path.basename(extracted_dir)}")
    log(f"[INFO]   device  : {serial or '<none detected>'}")
    if not dry_run and not serial:
        log(f"[WARN]   no matching device online for '{target}' — skipped")
        return True
    
    if kind == "big":
        return flash_big_extracted(target, extracted_dir, serial, log, dry_run)
    if kind == "small":
        return flash_small_extracted(target, extracted_dir, log, dry_run)
    if kind == "mcu":
        return flash_mcu_extracted(extracted_dir, serial, log, dry_run)
    if kind == "vlmu_mcu":
        return flash_vlmu_mcu_extracted(extracted_dir, serial, log, dry_run)
    log(f"[ERROR]   unknown job kind: {kind}")
    return False


# ---------------------------------------------------------------- 计划 / 主流程
def plan_jobs(prep_dir, targets, size_pref="auto"):
    """扫描目录, 返回有序 job 列表 [(kind, target, pkg_path), ...] (大包->小包->mcu)。"""
    cats = common.scan_dir(prep_dir)
    big, small, mcu = [], [], []
    for t in targets:
        if t == "mcu":
            p = common.newest_pkg(cats, "mcu")
            if p:
                mcu.append(("mcu", "mcu", os.path.join(prep_dir, p)))
            continue
        if t not in TARGET_CATS:
            continue
        bcat, scat = TARGET_CATS[t]
        b = common.newest_pkg(cats, bcat)
        s = common.newest_pkg(cats, scat)
        if size_pref != "small" and b:
            big.append(("big", t, os.path.join(prep_dir, b)))
        if size_pref != "big" and s:
            small.append(("small", t, os.path.join(prep_dir, s)))
    return big + small + mcu


def expand_targets(targets):
    out = []
    for t in targets:
        if t == "all":
            out.extend(ALL_ORDER)
        else:
            out.append(t)
    # 去重保序
    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def detect_and_log(log):
    """检测设备并打印, 返回 [(serial, mode), ...]。"""
    log("[OK] ==> Detecting connected devices...")
    devices = common.detect_devices()
    for s, m in devices:
        log(f"[INFO]   {s} -> ChipMode: {m}")
    if not devices:
        log("[WARN]   no adb devices online")
    log("")
    return devices


def execute_jobs(jobs, log=print, dry_run=False, force_serial="", devices=None):
    """按给定顺序执行 job 列表 [(kind, target, pkg_path, is_extracted), ...]。

    失败即停并返回 False (与 flash.sh 的 exit 语义一致)。全成功返回 True。
    devices 可传入已探测的 [(serial, mode)] 以跳过重复检测。
    is_extracted: True 表示 pkg_path 是已解压目录, 跳过解压步骤。
    """
    if devices is None:
        devices = detect_and_log(log)
    serial_map = build_serial_map(devices)

    ok = True
    for job in jobs:
        # 兼容旧格式 (3 元素) 和新格式 (4 元素)
        if len(job) == 3:
            kind, target, pkg = job
            is_extracted = False
        else:
            kind, target, pkg, is_extracted = job
        
        serial = serial_for_target(target, serial_map, force_serial)
        if is_extracted:
            # 已解压目录: work_root 就是目录本身
            work_root = os.path.dirname(pkg)
            if not _run_job_extracted(kind, target, pkg, serial, work_root, log, dry_run):
                ok = False
                break
        else:
            if not _run_job(kind, target, pkg, serial, _work_root(pkg), log, dry_run):
                ok = False
                break
        log("")
    return ok


def _work_root(pkg):
    return os.path.dirname(os.path.abspath(pkg))


def run_flash(prep_dir, targets, size_pref="auto", dry_run=False,
              force_serial="", log=print):
    """一键刷机。返回退出码 (0=成功)。targets 可含 'all'。"""
    log("")
    log("[OK] XP5 one-key flash (python)")
    log(f"[INFO] Prep folder : {prep_dir}")
    log(f"[INFO] Dry-run     : {dry_run}")
    log(f"[INFO] Size pref   : {size_pref}")
    log("")

    if not os.path.isdir(prep_dir):
        log(f"[ERROR] Prep folder not found: {prep_dir}")
        log("[ERROR] 先下载包: python3 downloader.py -p E29 -a <DT...>")
        return 1

    targets = expand_targets(targets)
    for t in targets:
        if t not in TARGET_CATS and t != "mcu":
            log(f"[ERROR] Unknown target: {t} (use xp5_a|xp5_b|vlmu|mcu|all)")
            return 1

    devices = detect_and_log(log)

    jobs = plan_jobs(prep_dir, targets, size_pref)
    if not jobs:
        log(f"[WARN] No packages matched in {prep_dir} for the requested target(s).")

    if not dry_run:
        os.makedirs(os.path.join(prep_dir, ".extracted"), exist_ok=True)

    log("[OK] ==> Flash order: 大包 -> 小包 -> MCU")
    log("")
    ok = execute_jobs(jobs, log=log, dry_run=dry_run,
                      force_serial=force_serial, devices=devices)
    if dry_run:
        log("[OK] Dry-run complete. No changes made.")
    elif ok:
        log("[OK] Flashing finished.")
    else:
        log("[ERROR] Flashing failed.")
    return 0 if ok else 1


# ---------------------------------------------------------------- CLI
def build_parser():
    p = argparse.ArgumentParser(
        prog="flasher.py",
        description="一键刷 XP5 刷机包 (flash.sh 的 Python 版)。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  flasher.py -n all                 # dry-run: 打印设备+包计划
  flasher.py vlmu                   # 刷 vlmu (自动大/小包 + 设备)
  flasher.py xp5_a xp5_b            # 刷两块 xp5
  flasher.py mcu                    # 经 SoC_A 设备刷 MCU
  flasher.py --small vlmu           # 强制只用小包
  flasher.py -d /path/to/pkgs all   # 指定待刷目录
""")
    p.add_argument("targets", nargs="*", help="xp5_a|xp5_b|vlmu|mcu|all (默认 all)")
    p.add_argument("-d", "--dir", default="",
                   help="待刷包目录 (默认 <script>/flash_prep)")
    p.add_argument("-s", "--serial", default="",
                   help="强制指定 adb serial (跳过 ChipMode 自动识别)")
    p.add_argument("--big", action="store_true", help="只用大包 (adc_flashall.sh)")
    p.add_argument("--small", action="store_true", help="只用小包 (deploy_xpilot_by_adb.sh)")
    p.add_argument("--pkg", action="append", default=[], metavar="KIND:TARGET:PATH",
                   help="可重复: 直接刷指定包 (供 GUI 单刷用), 跳过目录扫描。"
                        "KIND=big|small|mcu, TARGET=xp5_a|xp5_b|vlmu|mcu, PATH=包绝对路径")
    p.add_argument("-n", "--dry-run", action="store_true", help="只打印计划, 不实际刷")
    return p


def _parse_pkg_specs(specs, log=print):
    """把 ['KIND:TARGET:PATH', ...] 解析成 [(kind, target, path), ...]; 出错返回 None。"""
    jobs = []
    for spec in specs:
        parts = spec.split(":", 2)
        if len(parts) != 3 or not all(parts):
            log(f"[ERROR] 无效 --pkg (应为 KIND:TARGET:PATH): {spec}")
            return None
        kind, target, path = parts
        if kind not in ("big", "small", "mcu"):
            log(f"[ERROR] 无效 --pkg KIND: {kind} (用 big|small|mcu)")
            return None
        jobs.append((kind, target, path))
    return jobs


def main(argv=None):
    args = build_parser().parse_args(argv)

    # GUI 单刷: 直接给定包路径, 跳过目录扫描与 target 展开
    if args.pkg:
        jobs = _parse_pkg_specs(args.pkg)
        if jobs is None:
            return 2
        ok = execute_jobs(jobs, log=print, dry_run=args.dry_run,
                          force_serial=args.serial)
        print("[OK] Dry-run complete. No changes made." if args.dry_run
              else ("[OK] Flashing finished." if ok else "[ERROR] Flashing failed."))
        return 0 if ok else 1

    prep_dir = args.dir or common.default_prep_root()
    size_pref = "big" if args.big else ("small" if args.small else "auto")
    targets = args.targets or ["all"]
    return run_flash(prep_dir, targets, size_pref=size_pref,
                     dry_run=args.dry_run, force_serial=args.serial, log=print)


if __name__ == "__main__":
    sys.exit(main())
