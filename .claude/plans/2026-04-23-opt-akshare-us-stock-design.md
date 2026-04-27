# 设计文档：AKShare 美股数据获取升级

**日期：** 2026-04-23
**状态：** Draft

## 1. 背景与目标

当前 `AkshareFetcher`（Provider 层）不支持美股，直接抛出 `DataFetchError`。Processor 层的 `AkshareSource` 使用的是 `stock_us_daily` 接口。

本次升级目标：
- **Provider 层和 Processor 层**统一使用 akshare 的 `stock_us_hist`（东财数据源）获取美股日线数据
- 保留 `stock_us_daily` 作为回退数据源
- 返回并存储 `stock_us_hist` 接口的**所有字段**（振幅、涨跌额、换手率等）
- 统一使用纯 ticker（如 `AAPL`）作为输入，内部做东财格式转换
- 默认前复权（`adjust="qfq"`）

## 2. 改动范围

| 文件 | 改动内容 |
|------|----------|
| `src/provider/akshare_fetcher.py` | 新增 `_fetch_us_data`、`_to_us_em_symbol`；修改 `_fetch_raw_data` 美股分支；扩展 `_normalize_data` |
| `src/processor/us_daily/sources/akshare_source.py` | 重写 `fetch_daily`，使用 `stock_us_hist` + 回退；保留所有字段 |
| 测试文件 | 新增对应单测 |

## 3. Provider 层设计 (`akshare_fetcher.py`)

### 3.1 `_fetch_raw_data` 修改

将美股分支从直接抛异常改为调用新方法：

```python
if _is_us_code(stock_code):
    return self._fetch_us_data(stock_code, start_date, end_date)
```

### 3.2 新增 `_fetch_us_data` 方法

模仿 `_fetch_stock_data` 的多源回退模式：

```
_fetch_us_data(stock_code, start_date, end_date)
  1. 设置随机 User-Agent + 速率限制
  2. 将纯 ticker（如 AAPL）转换为东财格式（如 105.AAPL）
  3. 先试 ak.stock_us_hist(symbol=东财格式, period="daily", start_date=..., end_date=..., adjust="qfq")
  4. 失败则回退 ak.stock_us_daily(symbol=ticker, adjust="qfq")，并按日期范围过滤
  5. 返回原始 DataFrame
```

### 3.3 新增 `_to_us_em_symbol` 辅助函数

`stock_us_hist` 需要带市场前缀的代码（如 `105.AAPL`）。

```
_to_us_em_symbol(ticker)
  1. 检查内存缓存 self._us_code_map 是否有映射表
  2. 没有则调用 ak.stock_us_spot_em() 获取美股列表，建立 ticker → 完整代码的映射并缓存
  3. 查找返回，找不到则返回 None（触发回退到 stock_us_daily）
```

缓存放在实例变量 `self._us_code_map` 中，生命周期跟随 `AkshareFetcher` 实例。

如果 `stock_us_spot_em()` 调用失败或 ticker 不在列表中，直接跳过 `stock_us_hist`，回退到 `stock_us_daily`。

### 3.4 `_normalize_data` 扩展

当前列名映射仅覆盖 A 股常用字段。对美股数据，增加以下映射：

```python
us_column_mapping = {
    '日期': 'date',
    '开盘': 'open',
    '收盘': 'close',
    '最高': 'high',
    '最低': 'low',
    '成交量': 'volume',
    '成交额': 'amount',
    '涨跌幅': 'pct_chg',
    '振幅': 'amplitude',
    '涨跌额': 'change',
    '换手率': 'turnover_rate',
}
```

美股数据不做列筛选，保留所有已映射的列。判断方式：通过 `_is_us_code(stock_code)` 确定是否为美股，如果是则跳过 `keep_cols` 筛选。

## 4. Processor 层设计 (`us_daily/sources/akshare_source.py`)

### 4.1 `fetch_daily` 重写

```
fetch_daily(ticker, start_date, end_date)
  1. 将 ticker（如 AAPL）转为东财格式（内部实现 _to_us_em_symbol）
  2. 先试 ak.stock_us_hist(symbol=东财格式, period="daily", start_date=..., end_date=..., adjust="qfq")
  3. 失败则回退 ak.stock_us_daily(symbol=ticker, adjust="qfq")
  4. 日期范围过滤（stock_us_daily 返回全量数据需要过滤，stock_us_hist 自带范围参数但仍需兜底过滤）
  5. 列名中文转英文映射（与 Provider 层相同的映射表）
  6. 返回 DataFrame —— 包含所有字段，不再只筛选 STANDARD_COLUMNS
```

### 4.2 代码映射缓存

与 Provider 层相同的策略：通过 `ak.stock_us_spot_em()` 获取映射表并缓存在实例变量中。两层独立实现，不引入层间耦合。

### 4.3 `base.py` 不改动

`STANDARD_COLUMNS = ["date", "open", "high", "low", "close", "volume"]` 保持不变。`AkshareSource` 返回的 DataFrame 是 `STANDARD_COLUMNS` 的超集，额外字段作为扩展列。其他 Source（MassiveSource、YfinanceSource）不受影响。

### 4.4 数据存储

`agg_fetcher.py` 将 DataFrame 转为 JSON 写入文件。因为 DataFrame 包含更多字段，JSON 文件会自然包含所有字段（如 amount、pct_chg、amplitude、change、turnover_rate），无需改动存储逻辑。

## 5. 错误处理

- `_fetch_us_data` 内部捕获每个数据源的异常，记录日志后尝试下一个
- 两个源都失败时抛出 `DataFetchError`，由上层 `DataFetcherManager` 回退到 YfinanceFetcher / LongbridgeFetcher
- `stock_us_spot_em()` 缓存构建失败不阻塞流程，直接跳到 `stock_us_daily` 回退路径
- 速率限制和反爬策略复用现有的 `_enforce_rate_limit` 和 `_set_random_user_agent`

## 6. 测试计划

### Provider 层测试

mock `ak.stock_us_hist`、`ak.stock_us_daily`、`ak.stock_us_spot_em`，验证：

- 正常获取路径（stock_us_hist 成功）
- 回退路径（stock_us_hist 失败，stock_us_daily 成功）
- 全部失败抛出 DataFetchError
- 返回数据包含所有字段（amplitude、change、turnover_rate）
- 代码格式转换正确（AAPL → 105.AAPL）
- 代码映射缓存只调用一次 stock_us_spot_em

### Processor 层测试

mock akshare 接口，验证：

- stock_us_hist 优先调用
- 回退逻辑正常
- 返回 DataFrame 包含扩展字段
- JSON 存储包含所有字段
