# 抢机票 (farewatch)

定时查机票价格，低于你设的价格线就提醒。

## 它做什么

1. 读 `watchlist.yaml` 里的每条监控：出发机场、到达机场、去程和回程的日期窗口、舱位、是否只要直飞、价格线。
2. 向数据源查询当前在售的往返票。
3. 逐条复核舱位、直飞、日期和机场，不依赖数据源自己的筛选。
4. 把每次查到的最低价追加到历史记录。
5. 有票低于价格线时提醒：在 GitHub Actions 里是开一个 issue 并 @ 仓库所有者，本地运行是打印出来。

同一张票不会重复提醒。价格再降 1% 以上，或者 24 小时后票还在，才会再提醒一次。票消失后又出现，也会再提醒。

## 运行

```bash
uv sync
cp examples/watchlist.yaml watchlist.yaml   # 然后按需修改
uv run python -m farewatch scan
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--watchlist` | `watchlist.yaml` | 监控列表 |
| `--data-dir` | `data` | 存放 `history.jsonl`（历史）和 `state.json`（已经提醒过什么） |
| `--github-issues` | 关 | 用 GitHub issue 发提醒，需要环境变量 `GITHUB_REPOSITORY` 和 `GITHUB_TOKEN` |

退出码：0 正常；1 有数据源查询失败，或者提醒没发出去；2 配置有问题。

测试：`uv run pytest`

## 24 小时运行

`.github/workflows/farewatch.yml` 在 GitHub Actions 里跑同一条命令，历史记录存在 `farewatch-data` 分支。目前只能手动触发（Actions → farewatch → Run workflow）。要定时运行，在 workflow 的 `on:` 下面加：

```yaml
  schedule:
    - cron: "7,37 * * * *" # 每 30 分钟一次
```

定时运行只认默认分支上的 workflow 和 `watchlist.yaml`。

想确认提醒能送达：手动触发时把 watchlist 填成 `examples/smoke.yaml`，它的价格线高到任何票都算命中。

## 数据源和它的局限

目前只有 Kiwi.com 的公开 MCP 接口，不需要密钥。它给的是 Kiwi 这家 OTA 自己的售价，不是航司直销价：

- 2026 年 11 月拿同一组中美商务舱航班和 Expedia 对比，Kiwi 贵 5% 到 60%。
- 查不到美联航和达美。
- 一次查询最多返回 15 个最便宜的报价。

所以 Kiwi 的价格只能当上限看，下单前要去航司官网核对。

新增数据源：写一个 `fetch(watch, currency) -> list[Itinerary]`，登记到 `farewatch/cli.py` 的 `SOURCES`。
