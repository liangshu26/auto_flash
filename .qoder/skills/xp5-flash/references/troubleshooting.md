# 实测故障案例与排查

## 大包报 "Doesn't have a device with serial number 172.20.1.22"

```
[INFO] Flash BIG xp5_a via adc_flashall.sh (serial=172.20.1.22)
No connected fastboot devices detected.
Doesn't have a device with serial number 172.20.1.22.
Error: auto_fastboot_by_usb_id.sh failed
```

传进去的是 ssh 的 IP，不是 USB 序列号。成因链条：

1. 上一轮小包刷完，`deploy_xpilot_by_adb.sh` 结尾 `begin reboot xpu process...`，板子在重启
2. 重启途中 `adb shell version` 只吐出半截（到 `Built Time: 2026-...` 就断），没有 ChipMode 行
3. 重试次数不够 → 记成 `ChipMode: unknown`
4. USB 优先的逻辑靠 ChipMode 去重，`unknown` 挡不住 ssh 补位 → `172.20.1.22 -> SoC_A (ssh 网络)`
5. 大包的候选设备表没排除 ssh → IP 被当成序列号传给了 fastboot 脚本

**修：** 大包的候选设备表必须排除 ssh（只留 adb/fastboot）；ChipMode 读到截断输出时延长重试。
排除 ssh 之后 serial 为空，自然会落进"板子之前见过、现在不见了"的重启等待逻辑。

**注意连带效应：** 只要 ssh 补位成功，serial 就非空，基于"serial 为空"的等待逻辑不会触发，
板子一秒都不等就直接判跳过或跑错脚本。接入方式过滤必须在选 serial 那一步做掉。

## 大包偶发进不去 fastboot（可安全重跑）

```
Rebooting device <serial> (Board B) into fastboot mode...
waiting for device to enter fastboot mode... at 20261105_103148
timeout 60s ./platform-tools/linux/fastboot -s <serial> getvar board-type
rc=1
```

判据：成功时 `waiting for device to enter fastboot mode` 之后约 **10~12 秒**就会出现
`board_type: B vehicle_did: 0x........ at <timestamp>`；这一行始终没出现，只有包内
`timeout 60s ... getvar board-type`，就是没进 fastboot。

**这次失败发生在任何分区被写入之前**（日志里没有 `fastboot_flashall at`，也没有
`Sending` / `Flashing` / `Writing`），所以重跑没有变砖风险。

同一次任务里其它板子（C 用 10s、A 用 12s）都正常进 fastboot，而且这块板之前半小时
一直在线闲置、不是刚被上一个任务重启完 —— 属偶发。

日志本身**无法**区分三种成因，现象完全一样：

1. `adb reboot fastboot` 那条命令没生效，板子一直待在正常系统里
2. 板子进了 fastboot 但 USB 重新枚举，包内那个 `./platform-tools/linux/fastboot` 认不到
3. 板子进了 fastboot 又自己启动出去了（部分 bootloader 在 fastboot 下无人操作会超时续 boot）

要区分只能在失败瞬间同时看 `fastboot devices` 和 `adb devices`。

重跑前先归位：板子若还卡在 fastboot，`adb devices` 里就没有它，而 `adc_flashall.sh`
开头要靠 `adb devices` 列出各板板型，会直接报找不到设备。先
`fastboot -s <serial> reboot` 并等它回到 adb，再重跑整个脚本。

自动化时的重试门槛要用输出标记而不是退出码：**看到** `waiting for device to enter
fastboot mode`、**且没看到** `fastboot_flashall at` / `Sending '` / `Flashing ` /
`Writing ` 任一条，才允许重试。只判退出码会把刷到一半失败的情况也重刷一遍。

## ssh 刷机全部失败却报成功

```
kex_exchange_identification: Connection closed by remote host
Connection closed by 172.20.1.22 port 22
...
xp5_a ERROR: xpilot/version.txt missing EEA: field and remote /product/EEA_TYPE not found.
==== 完成 ====
```

`deploy_xpilot.sh` 每条 ssh 都失败，远端版本号全读成空，最后打一行 `ERROR:` 就 **exit 0**。

`ERROR: xpilot/version.txt missing EEA` 字面上像在说"PDK 太旧、要刷大包更新 product.img"，
**实际根因是远端文件根本没读到**。照提示去刷大包是错的方向。

判定：远端版本号为空 + `Connection closed by` 反复出现 = ssh 通道整个不可用。
常见原因是板子正在重启（探测那一刻恰好能登，随后 sshd 就断了）。

## adb 小包只推了 1409 个文件却报成功

```
adb: error: failed to copy './xpilot/perception/models/aeb/aeb.xprt.enc' ...: couldn't read from device
...（几十行）
1409 files pushed. 15.3 MB/s (1786739299 bytes in 111.330s)
adb push error
```

rc 仍为 0。对账：xp5 整包约 **12584 个文件 / 4.4 GB**，这里只推了 1409 个 / 1.8 GB。
失败点集中在 `perception/` 之后——`perception/` 是最大的目录（1340 files / 2.2 GB / 144s），
很可能是它把 adb 连接压断了。

判定必须扫 `adb push error` 横幅和 `adb: error: failed to copy`，光看 rc 会把刷坏的板子判成成功。

## 终端 ssh 不上，但工具显示设备在线

两个独立成因，要分开查：

**a) known_hosts 冲突**

```
@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @
Offending ECDSA key in /home/xpeng/.ssh/known_hosts:14
```

板子重刷后 host key 变了。终端修法：

```bash
ssh-keygen -f "$HOME/.ssh/known_hosts" -R "172.20.1.22"
```

自动化流程应当用 `-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null` 免疫。
`deploy_xpilot.sh` 自己也会 `remove known_hosts`，所以它能自愈而终端不能。

**b) 只探端口不真登**

TCP 连 22 端口成功 ≠ 能免密登录。端口通但免密 root 没配好、或这个 IP 被别的机器占着时，
探测说在线而所有实际命令都失败。探测必须真跑一次：

```bash
ssh -p 22 -o BatchMode=yes -o ConnectTimeout=3 \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR root@<ip> true
```

`BatchMode=yes` 禁止一切交互提示（密码 / host key），登不上立刻失败而不是挂住。

## 设备识别不到 / ChipMode unknown

按顺序排查：

1. `adb devices` 里 state 是不是 `device`（`unauthorized` / `offline` 都不算）
2. `adb -s <serial> shell version` 手跑一次，看输出完整不完整。
   只有前半段（PDK version / Version / Branch / Built Time）= 板子在开机，**等**，别判 unknown
3. `stderr` 报 `device '<serial>' not found` = 设备真掉了，重试几次即可放弃
4. adb server 被别的客户端（版本不匹配的 adb）重启过 → 输出会短暂失败，重试
5. adb 输出可能含 NUL 字节，GNU grep 会把整行当 binary 丢掉。先 `tr -d '\000\r'` 再 grep
6. 循环里调 adb 要 `</dev/null`，否则 adb 会吞掉循环的输入列表

板子已处于 fastboot 模式时 `adb devices` 看不到它，要用 `fastboot devices` +
`fastboot getvar board-type`（A→SoC_A, B→SoC_B, C→SoC_VLMU；输出在 **stderr**）。

## MCU 刷完不知道成没成

`aurix_v7_xp5.sh` 第二次刷写后只 `sleep 30` 就 `exit 0`，**没有任何校验**。
要确认自己跑：

```bash
adb -s <SoC_A serial> shell /xpilot/aurix_utility/aurix_utility -v
```

看 `Aurix active boot` 与版本号。第一轮刷写有校验（`FINISH_FLASH_ZONE` 必须等于
重读到的 `CURR_AURIX_ZONE`，不等就 `Switch failed!`），第二轮没有。

在 VLMU 板上跑会得到 `bash: line 1: /xpilot/aurix_utility/aurix_utility: No such file or directory`
和空的 master MCU 版本号——走错板子了，MCU 只能经 SoC_A。

## VLMU MCU 升级中途设备掉线

**正常现象。** 升级成功后 `update_service` 会 `SwitchReboot`，设备立即重启，
之后任何 adb 查询都只会拿到 `device not found`。不要据此判失败。

结果只能从日志读，而且要在启动升级**前**清空，否则读到上一次的残留：

```bash
adb -s <serial> shell "> /xdata/log/latest/update_service.txt.log"     # 升级前清空
adb -s <serial> shell "grep -E 'Upgrade progress 100%.*Success|FlashExit.*Success' \
    /xdata/log/latest/update_service.txt.log | tail -1"                # 判成功
adb -s <serial> shell "grep 'Fail!!!' /xdata/log/latest/update_service.txt.log | tail -1"   # 判失败
```

## 其它常见噪声（不是故障）

| 输出 | 说明 |
|---|---|
| `rm: cannot remove '/xpilot/...': Permission denied` | 每次成功刷机都有约 40 行 |
| `rm: cannot remove '/mnt/usb/.gitkeep': Read-only file system` | 只读挂载点，脚本会打 `Delete files in /mnt/usb failed, only printing, ignore it` |
| `tar: ... time stamp ... is ... s in the future` | 板子时钟不对（实测见到 md5 文件名是 `md5sum_20200108_002630`）|
| `/dev/block/by-name/xpilot alignment is offset by 499712 bytes` | 分区对齐提示，不影响刷写 |
| `./deploy_xpilot.sh: line 639: ./xp5_status.sh: No such file or directory` | 包里缺这个可选脚本 |
| `./deploy_xpilot_by_adb.sh: 行 131: 警告： 命令替换：忽略输入中的 null 字节` | adb 输出含 NUL |
| `sshpass is not installed, start install now` + `sudo: 需要密码` | 免密 ssh 可用时不影响 |
| `Warning: skip copying blackbox_a image avb footer due to sparse image.` | fastboot 对 sparse 镜像的正常提示 |
| `Device <serial> is still online after 300 retries.` | VLMU 小包收尾等不到 adb 掉线，之后照常 `reboot xpu` + `Done`；实测两次独立刷机都这样且都成功 |
| `[WARN] ... 版本号: X 与...不一致, 请确认是否影响` | bsp/mcu 版本比对警告，脚本会继续刷 |
