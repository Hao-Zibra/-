# 抢机票 (farewatch)

两件事：按需把某趟行程的最低价查出来（`hunt`），以及定时盯价、低于价格线就提醒（`scan`）。

## 安装

```bash
uv sync
```

测试：`uv run pytest`

## 按需搜索：`hunt`

给出发地、目的地、去程和回程的日期窗口，它把窗口里每一天都查到，列出最便宜的走法。

```bash
uv run python -m farewatch hunt \
  --from HKG,PVG,PEK,CAN,XMN,TFU --to LAX \
  --depart 2027-03-10:2027-03-16 --return 2027-04-07:2027-04-13
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--from` / `--to` | 必填 | 机场三字码，逗号分隔 |
| `--depart` / `--return` | 必填 | 一天，或 `起:止` 的日期窗口 |
| `--cabin` | `business` | `economy`、`premium_economy`、`business`、`first` |
| `--connections` | 关 | 也接受转机（默认只要直飞） |
| `--same-airport` | 关 | 回程必须回到出发的那个机场（默认可以回到任一出发地） |
| `--adults` | 1 | 成人人数 |
| `--currency` | `USD` | 报价货币 |
| `--top` | 10 | 列出多少种走法 |

输出包括：最便宜的若干种走法、每个出发地各自的最低价、最低价那组航班在哪些其他日期实查也是同价，以及下单链接。

它在 Skiplagged 上分三步查，因为那里一次只能查一天：

1. 两个窗口里每一天的单程价。
2. 把最便宜的单程两两配成"两张单程票"的走法。
3. 往返票通常比两张单程便宜得多，但每组日期都要单独查。所以先按单程价挑出每家航司最可能便宜的日期去查往返价，再把领先的两家航司其他同价位的日期逐一实查。

一次搜索大约 60 到 75 次查询，用时 1 到 3 分钟。Skiplagged 会限流：2026 年 11 月实测，半小时内约 150 次查询后开始拒绝（429）。所以不要连着跑；报告里出现限流提示时，等二三十分钟再查。预计查询数超过 120 的搜索会被直接拒绝，这时请减少机场或缩短日期窗口。

退出码：0 正常；1 一张票都没找到且有搜索失败；2 参数有问题。

## 定时盯价：`scan`

```bash
cp examples/watchlist.yaml watchlist.yaml   # 然后按需修改
uv run python -m farewatch scan
```

1. 读 `watchlist.yaml` 里的每条监控：出发机场、到达机场、去程和回程的日期窗口、舱位、是否只要直飞、价格线。
2. 向数据源查询当前在售的往返票。
3. 逐条复核舱位、直飞、日期和机场，不依赖数据源自己的筛选。
4. 把每次查到的最低价追加到历史记录。
5. 有票低于价格线时提醒：在 GitHub Actions 里是开一个 issue 并 @ 仓库所有者，本地运行是打印出来。

同一张票不会重复提醒。价格再降 1% 以上，或者 24 小时后票还在，才会再提醒一次。票消失后又出现，也会再提醒。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--watchlist` | `watchlist.yaml` | 监控列表 |
| `--data-dir` | `data` | 存放 `history.jsonl`（历史）和 `state.json`（已经提醒过什么） |
| `--github-issues` | 关 | 用 GitHub issue 发提醒，需要环境变量 `GITHUB_REPOSITORY` 和 `GITHUB_TOKEN` |

退出码：0 正常；1 有数据源查询失败，或者提醒没发出去；2 配置有问题。

### 24 小时运行

`.github/workflows/farewatch.yml` 在 GitHub Actions 里跑 `scan`，历史记录存在 `farewatch-data` 分支。目前只能手动触发（Actions → farewatch → Run workflow）。要定时运行，在 workflow 的 `on:` 下面加：

```yaml
  schedule:
    - cron: "7,37 * * * *" # 每 30 分钟一次
```

定时运行只认默认分支上的 workflow 和 `watchlist.yaml`。

想确认提醒能送达：手动触发时把 watchlist 填成 `examples/smoke.yaml`，它的价格线高到任何票都算命中，提醒标题会写明是送达测试。

## 数据源

| 数据源 | 用在 | 特点 | 局限 |
|---|---|---|---|
| Skiplagged 公开 MCP 接口 | `hunt` | 有美联航、达美等 Kiwi 没有的航司；2026 年 11 月抽查，往返价和 Expedia 相差不到一美元 | 一次只能查一天；会限流；往返结果只标明去程航班，回程航班要下单时确认 |
| Kiwi.com 公开 MCP 接口 | `scan`、`hunt` | 一次查询覆盖整个日期窗口 | 是 Kiwi 这家 OTA 自己的售价，同一组航班比 Expedia 贵 5% 到 60%；查不到美联航和达美；一次最多 15 个报价 |

两个接口都不需要密钥。`scan` 目前只用 Kiwi，所以定时盯价看不到美联航和达美的票。

任何来源的价格下单前都要去航司官网核对。

新增一次能查整个日期窗口的数据源：写一个 `fetch(watch, currency) -> list[Itinerary]`，登记到 `farewatch/cli.py` 的 `SOURCES`（定时盯价）和 `farewatch/hunt.py` 的 `RANGE_SOURCES`（按需搜索）。
