# 包内脚本输出序列对照

用于定位"刷到哪一步卡住了"。以下序列均来自实测日志。

## adc_flashall.sh \<serial\>

```
flash with input usb_id
ADB device <serial> : A                     <- 列出所有 adb 设备及其板型
lcs: DM                                     <- 生命周期状态检查
Rebooting device <serial> (Board A) into fastboot mode...
waiting for device to enter fastboot mode... at <timestamp>
board_type: A vehicle_did: 0x0000000c at <timestamp>
RPU_META1: <pkg>/flash_tools/../ufs_images/rpu_meta1_E29.bin
no board_type input, use fastboot getvar board-type
no rpu_type input, use fastboot getvar board-did
fastboot_flashall at <timestamp>
==================================================================================
product: xp5-apusec-xccp
Detected board-id: 0x0000000c
Board ID 0x0000000c is supported.
lcs: DM
Flash allowed.
==================================================================================
Flashing norflash partitions                <- slot a 全部 nor_* 分区，再 slot b
Flashing norflash complete
fastboot_flash_si: <N> s
Flashing ufs partitions
Stage 1: Skip ufs_provision/gpt_table (use -p to enable)
Stage 2: Flash ufs partitions according to slot
< waiting for <serial>>
Flashing ufs complete
==================================================
!!! Flashing norflash and ufs finished successfully !!!
total time statistics: <N> s
==================================================
fastboot_flashall end and start adc_flash_lu1 at <timestamp>
skip gpt_main0.bin flash (use -p to enable)
start to flash product.img
Partition: xpilot, File: xpilot.img ,path: ../ ,md5: <md5>
File xpilot.img found in directory ../ ../ufs_images/xp5A for partition xpilot.
start to flash xpilot                       <- 35 个 sparse 分片 × slot a/b
Rebooting
[<time>] Waiting 20s for device to boot...
[<time>] Checking /xdata/config/.no_auto_launch via adb (-s <serial>)...
[<time>] .no_auto_launch check result: not_found
[<time>] success: .no_auto_launch check completed.
flash all done and fastboot continue at <timestamp>
```

norflash 分区清单（每个都刷 a/b 两个 slot）：
`nor_romconfig` `nor_ptable` `nor_loaderconfig` `nor_spu_loader` `nor_ddr_drv`
`nor_spu_monitor0` `nor_spu_fw0` `nor_spu_monitor1` `nor_spu_fw1`
`nor_shsm_runtime` `nor_ahsm_runtime` `nor_apu_secloader` `nor_ddr_fw` `nor_misc`

ufs 分区清单：
`ai_hsm_fw` `rpu_fw1..4` `rpu_meta1..4` `scp_fw` `apu_monitor` `apu_teeos`
`apu_loader` `apu_linux` `apu_dtb` `vbmeta` `system`(9 分片) `vendor`
`blackbox` `userdata` `pstore`(erase)

卡在哪一步的判断：
- 停在 `waiting for device to enter fastboot mode` → 板子没进 fastboot，查 USB 线/供电
- `Board ID ... is not supported` → 板型与包不匹配
- 停在某个 `Sending sparse 'xpilot_a' N/35` → USB 传输中断或 /tmp 空间不足
- 有 `Flashing ufs complete` 但没有 `adc_flash_lu1` → norflash/ufs 阶段已失败

`xp5A` / `xp5B` 由 board-type 自动决定，同一包刷 A/B 两块板都正确。

## deploy_xpilot_by_adb.sh \<xp5_a|xp5_b|xp5_vlmu\>

```
./deploy_xpilot_by_adb.sh: 行 131: 警告： 命令替换：忽略输入中的 null 字节   <- 正常噪声
kernel_dts is xp5_a                          <- 由类型参数推出板型
Found device: <serial>
stop all apps
launch_mode is auto_launch
start to reboot for no_auto_launch
begin reboot xpu process...
wait device <serial> offline
Device <serial> is offline.
wait device <serial> online
Device <serial> is online.
start to scp xp5 package
rm: cannot remove '/xpilot/...': Permission denied      <- 约 40 行，正常噪声
./xpilot/about.txt: 1 file pushed. ...
./xpilot/<每个子目录>/: N files pushed. <速率> (<字节> in <秒>)
...
12584 files pushed. 14.2 MB/s (4446448869 bytes in 298.873s)
sync
begin reboot xpu process...
wait device <serial> offline
Device <serial> is offline.
===== Copying SW apps Done =====
time: <N> s
```

对账基准（xp5 整包）：约 **12584 个文件 / 4.4 GB / 300s**。
vlmu 小包约 **1674 个文件**。数字差一个量级就是中途断了。

大目录耗时参考：`perception/` 1340 files ≈ 144s、`usr/` 7932 files ≈ 21s、
`chief/` 269 files ≈ 26s、`libs/` 80 files ≈ 15s、`motion_control/` 134 files ≈ 11s。

## deploy_xpilot.sh \<xp5_a|xp5_b|xp5_vlmu\>（ssh 版）

```
deploy xp5 start and ip: 172.20.1.22
==========xp5_a print and check version==========
Old Version:
Get 172.20.1.22 SOC version:
Get 172.20.1.22 BSP version:
Get master MCU version:
PKG_BSP_VERSION_FILE: ./xpilot/bsp_version.txt
PKG_BSP_VERSION: 1.8.19.0
XPU_BSP_VERSION: <远端读到的>
xp5_a 当前的 bsp 版本号: X 与要刷的小包依赖的 bsp 的版本号: Y 不一致, ... 如不确认请刷大包
xp5_a 当前的 master mcu 版本号: X 与要刷的小包依赖的 mcu 的版本号: A.3.3 不一致, ... 如不确认请同步刷写mcu
remove known_hosts done
<逐文件 scp / md5 比对>
Files processed: 12554
✅ ALL md5sum files are identical
deploy_xpilot.sh: compare_xpilot_md5 success
===== Copying SW apps Done =====
```

耗时参考：vlmu 约 154s，xp5_a 约 234s。

**ssh 全部失败时长这样**（注意脚本仍然 `exit 0`）：

```
Get 172.20.1.22 SOC version:
sshpass is not installed, start install now
sudo: a terminal is required to read the password; ...
sudo: 需要密码
kex_exchange_identification: Connection closed by remote host
Connection closed by 172.20.1.22 port 22
...（每个版本查询都重复一遍）
XPU_BSP_VERSION:                      <- 空
xp5_a 当前的 bsp 版本号:  与要刷的小包依赖的 bsp 的版本号: 1.8.19.0 不一致, ...
xp5_a 当前的 master mcu 版本号:  与...A.3.3 不一致, ...
remove known_hosts done
xp5_a 当前xpilot/version.txt 缺少 EEA: 字段，并且远程 /product/EEA_TYPE 未找到。需要升级到最新的PDK以更新product.img。
xp5_a ERROR: xpilot/version.txt missing EEA: field and remote /product/EEA_TYPE not found....
```

识别特征：**版本号全为空** + `Connection closed by` 反复出现。
这条 `ERROR:` 行看起来像在说"PDK 太旧"，实际根因是远端文件根本没读到，
不要照着提示去升级 PDK。

## aurix_v7_xp5.sh \[-s SERIAL\] \[S\]

```
Using adb device: <serial>                   <- 未指定 -s 时先打印自动探测过程
pushing <x_APPA.hex> and <x_APPB.hex> to /xdata/latest_aurix/
Currently Running on Zone: A
Currently Flashing Zone: B
Starting aurix_utility
FINISH_FLASH_ZONE = 2
<sleep 30 + 等设备回来>
FINISH_FLASH_ZONE: 2
CURR_AURIX_ZONE: 2                           <- 两者必须相等，否则 Switch failed!
Currently Running on Zone: B
Currently Flashing Zone: A
Starting aurix_utility
<sleep 30，无校验，exit 0>
```

`aurix_utility -v` 的输出里靠 `Aurix active boot` 那一行判区：含 `:A` → 当前跑 A 区，
含 `:B` → 当前跑 B 区。读不到这行（例如 VLMU 板上 `/xpilot/aurix_utility` 不存在，
报 `bash: line 1: /xpilot/aurix_utility/aurix_utility: No such file or directory`）
就是走错了板子——MCU 只能经 SoC_A。
