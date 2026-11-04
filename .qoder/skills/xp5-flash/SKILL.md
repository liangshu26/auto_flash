---
name: xp5-flash
description: XPeng XP5 板子刷机的完整操作流程 —— 设备识别 (ChipMode / board-type / ssh)、刷机包分类、大包 adc_flashall.sh、小包 deploy_xpilot*.sh、MCU Aurix A/B 双区、VLMU MCU OTA 的执行步骤与成败判定。当需要刷 XP5 / XCCP / VLMU / Aurix 板子、判断某个刷机包该用哪个脚本和参数、分析刷机日志判断成功还是失败、或排查 ChipMode 识别与设备掉线问题时使用。
---

# XP5 刷机流程

## Overview

一套 XP5 台架上有两块 SoC 板 (A/B) 和一块 VLMU 板，Aurix MCU 只能通过 SoC_A 访问。
刷写分四类：大包 (fastboot 全量刷分区)、小包 (部署 /xpilot 应用)、MCU (Aurix A/B 双区)、
VLMU MCU (OTA 升级)。四类的设备接入方式限制各不相同，选错就会静默失败。

## 1. 设备拓扑

| ChipMode | 刷机目标 | board-type | 实测 board-id | ssh IP (免密 root) |
|---|---|---|---|---|
| `SoC_A` | `xp5_a` | A | `0x0000000c` | 172.20.1.22 |
| `SoC_B` | `xp5_b` | B | `0x00000014` | 172.20.1.30 |
| `SoC_VLMU` | `vlmu` | C | — | 172.20.1.119 |

`board-type` D = fpu，没有刷机目标。

识别命令：

```bash
adb devices                                        # 只取 state=device 的行
adb -s <serial> shell version                       # 找 "Current ChipMode: SoC_A"
fastboot devices
fastboot -s <serial> getvar board-type              # 注意 getvar 输出在 stderr
```

### 硬性约束（选错必失败）

- **Aurix MCU 只能通过 SoC_A 板访问**，`/xpilot/aurix_utility` 只装在这块板上。
  A 和 B **共用同一个 Aurix**，所以刷 xp5_b 时不需要、也不应该刷 MCU。
- master / slave Aurix 是两颗芯片，都经 SoC_A 访问，slave 用 `--m=1` 区分。
- **大包只能走 USB (adb/fastboot)**。把 ssh 的 IP 当序列号喂给 `adc_flashall.sh`
  必然得到 `Doesn't have a device with serial number <IP>`。
- **MCU 只能走 adb**，不支持 fastboot 模式，也不支持 ssh。
- 小包 adb / ssh 都可以，但设备处于 fastboot 模式时不能刷小包。
- 探测 ssh 设备在线要**真登一次**（`ssh -o BatchMode=yes root@<ip> true`）。
  只 TCP 连 22 端口会误判：端口通但免密没配好、或 IP 被别的机器占着时，
  看起来在线而实际所有 ssh 命令都会失败。

### ChipMode 读不到时

板子正在开机时 `adb shell version` 会**只吐出前半段**（到 `Built Time: 2026-...`
就断），没有 ChipMode 行。这种情况**必须重试**（实测需要 10 次 × 3s 量级），
不能直接判 `unknown`：一旦记成 unknown，同 IP 的 ssh 设备就会补位顶替这块板，
后续大包任务于是拿到一个 IP 当 fastboot 序列号。

区分两种失败：`stderr` 有报错（如 `device '<serial>' not found`）说明设备真不在，
重试几次即可放弃；`stdout` 有 version 内容但缺 ChipMode 才是开机截断，要延长重试。

## 2. 刷机包分类

按文件名前缀判断，**大小写敏感**：

| 前缀 | 类别 | 刷写方式 |
|---|---|---|
| `XCCP*` | xp5 大包 | `adc_flashall.sh` |
| `xpilot*` | xp5 小包 | `deploy_xpilot*.sh` |
| `VLMU<数字>*` 且名字含 `ENG` | vlmu 大包 | `adc_flashall.sh` |
| `vlmu*` | vlmu 小包 | `deploy_xpilot*.sh` |
| `XPilot_AURIX*` | MCU | `aurix_v7_xp5.sh` |
| `VLMUMCU_*`、`*_VLMU_*` | VLMU MCU | `update_service` OTA |

`VLMU` 大包必须同时满足"VLMU 后紧跟数字"和"含 ENG"，否则会把
`VLMU_EEA36_*_ULTRA.zip` 这类非刷机产物误当成大包。

小包传给 deploy 脚本的类型参数是 `xp5_a` / `xp5_b` / **`xp5_vlmu`**
（注意 vlmu 小包的参数是 `xp5_vlmu`，不是 `vlmu`）。

## 3. 执行顺序与设备重探

多目标一起刷时严格按 **大包 → 小包 → MCU**，任一失败即停，不要继续后面的目标。
大包会重写整个分区（含 product.img 与 xpilot 分区），小包只覆盖 /xpilot 下的应用文件，
顺序反了小包会被大包冲掉。

**每个任务开始前重新探测设备。** 上一个任务结尾通常会让板子重启
（大包 `Rebooting`、小包 deploy 结尾 `begin reboot xpu process...`），
旧快照必然过期——典型错误是 fastboot 刷完重启进 adb，却仍被当成 fastboot 而跳过小包。

板子重启回来要 1~2 分钟。判据用"**这块板之前探测到过、现在不见了**"：
之前见过 → 视为正在重启，轮询等待（上限 300s，对齐 `aurix_v7_xp5.sh` 的
`wait_device_online` 60×5s；轮询间隔 10s，因为每轮探测本身要几秒）。
从头到尾没出现过的板 → 立即跳过，白等只会拖慢整批任务。

注意"设备在线但接入方式不对"（如只有 ssh 而任务是大包）会让 serial 非空，
从而绕过基于"serial 为空"的等待逻辑；接入方式过滤要在选 serial 的那一步就做掉。

## 4. 大包 — adc_flashall.sh

```bash
tar -xzf <pkg>.tar.gz -C <work>
# 解压后层级不固定，用 find 定位，不要假设深度
flashall=$(find <work> -type f -name adc_flashall.sh -path '*flash_tools*' | head -1)
cd "$(dirname "$flashall")"
chmod +x adc_flashall.sh
./adc_flashall.sh <USB serial>
```

脚本自带完整流程，**不需要手动进 fastboot**：

1. `lcs: DM` 安全检查 → `Flash allowed.`
2. `auto_fastboot_by_usb_id.sh` 把板子推进 fastboot 模式
3. `fastboot getvar board-type` / `board-did` 自动判板型
4. `fastboot_flashall`：norflash 分区 slot a + slot b，再 ufs 分区 slot a + slot b（`--slot all`）
5. `adc_flash_lu1`：`product.img`（product_a/product_b），再 `xpilot.img` ——
   **按板型自动选 `../ufs_images/xp5A` 或 `xp5B`**，35 个 sparse 分片 × 双 slot
6. `Rebooting` → `Waiting 20s for device to boot...` → 检查 `/xdata/config/.no_auto_launch`

成功标志：

```
!!! Flashing norflash and ufs finished successfully !!!
total time statistics: <N> s
...
flash all done and fastboot continue at <timestamp>
```

前置条件：
- 依赖包内 `platform-tools/linux/fastboot`，**只能在 Linux 上刷**。
- `/tmp` 需有空间暂存大镜像（xpilot.img 约 5GB），刷前检查。
- 同一个 XCCP 大包 A/B 两块板通用，靠 board-type 自动区分镜像目录。

## 5. 小包 — deploy_xpilot*.sh

同一份包内有两个脚本，参数都是 `xp5_a|xp5_b|xp5_vlmu`：

- `deploy_xpilot_by_adb.sh` — USB 直连，**首选**
- `deploy_xpilot.sh` — 走网络 ssh/scp（固定 IP 免密 root）

```bash
tar -xzf <pkg>.tar.gz -C <work>
deploy=$(find <work> -type f -name deploy_xpilot_by_adb.sh | head -1)
cd "$(dirname "$deploy")"
chmod +x deploy_xpilot_by_adb.sh
./deploy_xpilot_by_adb.sh xp5_a
```

**adb 版流程**：`kernel_dts is xp5_a` → `Found device: <serial>` → `stop all apps` →
reboot 进 no_auto_launch（等 offline 再等 online）→ `start to scp xp5 package`
（先 `rm` 旧文件，再逐目录 `adb push`）→ `sync` → `begin reboot xpu process...`

**ssh 版流程**：读远端 SOC / BSP / master MCU 版本并与包内 `./xpilot/bsp_version.txt`
比对 → 校验 `xpilot/version.txt` 的 `EEA:` 字段 → `remove known_hosts` →
逐文件 scp → md5 逐文件比对。

成功标志：`===== Copying SW apps Done =====`。
ssh 版还要求 `✅ ALL md5sum files are identical` + `compare_xpilot_md5 success`，
且 `Files processed:` 两侧数量一致（xp5 约 12554，vlmu 约 1674）。
`files pushed` 数量也能对账：xp5 整包约 12584 个文件 / 4.4GB，只推了 1409 个就是中途断了。

版本不一致的 `[WARN]`（bsp / mcu 版本号对不上）**不是失败**，脚本会继续刷；
但 `ERROR: xpilot/version.txt missing EEA` 是硬中止，什么都没刷。

## 6. MCU — XPU Aurix A/B 双区

Aurix 不能刷自己正在执行的区，所以流程永远是：**先刷对侧区 → reboot 切区 →
校验区确实切过去了 → 再刷原来那侧**，两轮下来双区都是新固件。

hex 文件来自 `XPilot_AURIX*` 包，解压后取 `*_APPA.hex` 与 `*_APPB.hex` 各一个
（找到多个时取一个即可，两者必须配套同一次构建）。

### 6.1 用 aurix_v7_xp5.sh

```bash
tar -xzf XPilot_AURIX*.tar.gz -C <work>
cp "$(find <work> -type f -name '*_APPA.hex' | head -1)" <aurix 脚本目录>/
cp "$(find <work> -type f -name '*_APPB.hex' | head -1)" <aurix 脚本目录>/
cd <aurix 脚本目录>
./aurix_v7_xp5.sh -s <SoC_A 的 serial>      # 末尾加 S 刷 slave Aurix (--m=1)
```

`-s` 省略时脚本自己遍历 adb 设备找 `ChipMode == SoC_A` 的那个。
两个 hex **必须和脚本同目录**（脚本用 `ls ./ | grep -E '_APPA\.hex$'` 找）。

脚本内部就是 6.2 的七步，额外做了两件自动化：刷完 `sleep 30` 并等设备回online；
第一轮刷完校验区已切换，不等则 `Switch failed! Cannot Start Flash!` 退出 1。

**注意最后一轮刷完脚本不做任何校验就 `exit 0`**，第二次刷写成没成功它不管，
要确认得自己再跑一次 `aurix_utility -v` 看版本。

### 6.2 没有 aurix_v7_xp5.sh 时，直接用 adb 刷

全部命令都在台架 PC 上执行，`DEV` 是 **SoC_A** 板的 adb serial。

**第 0 步 — 找到 SoC_A 设备**

```bash
for s in $(adb devices | awk 'NR>1 && $2=="device" {print $1}'); do
  printf '%s -> ' "$s"
  adb -s "$s" shell version </dev/null 2>/dev/null | tr -d '\000\r' \
    | grep -i "Current ChipMode" | head -1 | sed 's/.*ChipMode:[[:space:]]*//'
done
```

取打印 `SoC_A` 的那个 serial。`/xpilot/aurix_utility` 只装在 SoC_A 板上，
在别的板上跑会得到 `No such file or directory` 和空的版本号。

```bash
DEV=<SoC_A 的 serial>
SLAVE=""            # 刷 slave Aurix 时改成 " --m=1"（注意前导空格）
AURIX=/xpilot/aurix_utility/aurix_utility
FLASHDRV=/xpilot/aurix_utility/FlashDrv/FlashDrv.hex
REMOTE=/xdata/latest_aurix
```

**第 1 步 — 确认设备在线**

```bash
adb -s "$DEV" get-state          # 必须输出 device
```

不是 `device` 就每 5s 重试一次，最多 60 次（300s）。

**第 2 步 — 推送 hex**

```bash
APPA=$(find <work> -type f -name '*_APPA.hex' | head -1)
APPB=$(find <work> -type f -name '*_APPB.hex' | head -1)
adb -s "$DEV" shell 'chmod 777 /xdata'
adb -s "$DEV" shell "rm -rf $REMOTE"
adb -s "$DEV" shell "mkdir -p $REMOTE"
adb -s "$DEV" push "$APPA" "$REMOTE/"
adb -s "$DEV" push "$APPB" "$REMOTE/"
```

先解析出确切文件名再 push，不要用 `adb push ./*_APPA.hex` —— 匹配到多个时会全推上去，
后面 `--update` 就无法确定用哪个。

**第 3 步 — 读当前运行区**

```bash
adb -s "$DEV" shell "$AURIX -v$SLAVE" | tr -d '\r'
```

在输出里找含 `Aurix active boot` 的那一行：含 `:A` → 当前跑 **A 区**；
含 `:B` → 当前跑 **B 区**；两者都没有 → 停止，读不到区就不能刷。

**第 4 步 — 刷对侧区**

当前跑 A 就刷 `*_APPB.hex`，当前跑 B 就刷 `*_APPA.hex`：

```bash
adb -s "$DEV" shell "$AURIX --update=$REMOTE/$(basename "$APPB"),$FLASHDRV --reboot=1$SLAVE"
```

`--update` 是**两个路径用逗号连接**：应用 hex + FlashDrv.hex，缺一不可。
`--reboot=1` 让 Aurix 刷完立即重启切区，**adb 会当场断开，这条命令的退出码不可信**，
不要据此判失败。

**第 5 步 — 等设备回来**

```bash
sleep 30
adb -s "$DEV" get-state          # 轮询直到输出 device，上限 300s
```

**第 6 步 — 校验区已切换（关键，不能跳）**

```bash
adb -s "$DEV" shell "$AURIX -v$SLAVE" | tr -d '\r' | grep "Aurix active boot"
```

现在必须显示**刚刷的那个区**（第 4 步刷了 B 就得显示 `:B`）。
不一致说明切区失败 —— **立即停止**，不要继续刷另一侧，否则两个区可能都是坏的。

**第 7 步 — 刷另一侧**

即原来在跑的那一侧（第 4 步刷了 B，这里就刷 A）：

```bash
adb -s "$DEV" shell "$AURIX --update=$REMOTE/$(basename "$APPA"),$FLASHDRV --reboot=1$SLAVE"
sleep 30
```

**第 8 步 — 自己校验（脚本版没有这一步）**

等设备回来后确认版本与运行区：

```bash
adb -s "$DEV" get-state
adb -s "$DEV" shell "$AURIX -v$SLAVE" | tr -d '\r'
```

核对版本号是刚刷的那个，且 `Aurix active boot` 落在预期区上。

### 补充

- **刷 xp5_b 不需要刷 MCU**：A/B 两块 SoC 共用同一个 Aurix，而它只能经 SoC_A 访问。
- master 与 slave 是两颗独立的 Aurix 芯片，都经 SoC_A 访问，靠 `--m=1` 区分；
  要两颗都更新就把上面第 3~8 步用 `SLAVE=""` 和 `SLAVE=" --m=1"` 各跑一遍。
- 整个过程中设备会重启两次，adb 断开属正常现象。

## 7. VLMU MCU — OTA 升级

```bash
adb -s <vlmu serial> push VLMUMCU_xxx.zip /tmp/
adb -s <vlmu serial> shell killall -9 update_service
# 然后在设备上执行：
source /xpilot/xlauncher/scripts/env.bash
/xpilot/update/update_service -R -i /tmp/VLMUMCU_xxx.zip
```

外层包（`*_VLMU_*.zip`）要先解压，OTA 包在 `OTAPackage/VLMUMCU_*.zip`；
`VLMUMCU_*.zip` 本身可直接用，不必解压。找到多个 `VLMUMCU_*` 时取 mtime 最新的那个。

进度和结果**只能从设备日志读**——升级过程 stdout 没有输出，成功后会 `SwitchReboot`
立刻掉线，之后任何 adb 查询都只会拿到 `device not found`：

```bash
adb -s <serial> shell "grep 'Current upgrade progress' /xdata/log/latest/update_service.txt.log | tail -1"
```

- 成功：`Upgrade progress 100%.*Success` 或 `FlashExit.*Success`，随后 `SwitchReboot`
- 失败：日志里出现 `Fail!!!`
- **设备重启掉线不代表失败**，先读日志再判。启动升级前先清空日志文件
  （`> /xdata/log/latest/update_service.txt.log`），否则会读到上一次的残留结果。

## 8. 成败判定（最容易踩坑）

**退出码不可全信。** 包内的 deploy 脚本会吞掉失败然后 `exit 0`：

| 输出现象 | rc | 真相 |
|---|---|---|
| `adb push error`，只推了 1409/12584 个文件 | 0 | **失败** |
| 每条 ssh 都 `kex_exchange_identification: Connection closed by remote host` | 0 | **失败** |
| 结尾 `ERROR: xpilot/version.txt missing EEA` | 0 | **失败**（硬中止，没刷任何东西） |

判定小包成败必须**同时**看退出码和输出里的硬失败标记：

```
adb push error                          # 脚本自己的失败横幅
adb: error: failed to copy              # adb push 逐文件报错
ssh: connect to host                    # ssh 连不上
Permission denied (publickey            # ssh 免密没配好
kex_exchange_identification             # 密钥交换阶段就被断开（设备重启中）
Connection closed by                    # ssh/scp 被远端关闭
lost connection                         # scp/sftp 中断
ERROR: xpilot/version.txt missing EEA   # 脚本自己的中止行（前缀是 xp5_a/xp5_b/xp5_vlmu，故不带前缀匹配）
```

**反过来，下列是正常噪声，绝不能当失败：**

- 几十行 `rm: cannot remove '/xpilot/...': Permission denied` —— 成功那次也有，
  所以**裸 `Permission denied` 不能当标记**
- 几百行 `tar: ... time stamp ... is ... s in the future` —— 板子时钟不对而已
- `Warning: skip copying blackbox_a image avb footer due to sparse image.`
- `./deploy_xpilot.sh: line 639: ./xp5_status.sh: No such file or directory`
- `sshpass is not installed, start install now` + `sudo: 需要密码` —— 脚本想装 sshpass
  但没权限；免密 ssh 可用时不影响刷机

大包和 MCU 用退出码判定即可（`adc_flashall.sh` 失败会返回非 0，
例如 `Error: auto_fastboot_by_usb_id.sh failed` → rc=1）。

## Resources

- `references/package-scripts.md` — 包内脚本 (`adc_flashall.sh` / `deploy_xpilot_by_adb.sh` /
  `deploy_xpilot.sh`) 的完整输出序列对照，用于定位刷到哪一步卡住
- `references/troubleshooting.md` — 实测过的故障案例与排查手段
