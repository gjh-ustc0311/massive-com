# AKShare 美股数据获取升级 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 AkshareFetcher（Provider 层）和 AkshareSource（Processor 层）支持通过 `stock_us_hist` 接口获取美股日线数据，保留 `stock_us_daily` 作为回退，返回并存储所有字段。

**Architecture:** 两层独立改造：Provider 层在 `AkshareFetcher` 新增 `_fetch_us_data` 方法实现美股获取 + 回退；Processor 层重写 `AkshareSource.fetch_daily` 使用相同策略。两层各自维护东财代码映射缓存（`stock_us_spot_em`），互不耦合。

**Tech Stack:** Python, akshare, pandas, unittest.mock

---

### Task 1: Provider 层 — 新增 `_to_us_em_symbol` 代码映射方法

**Files:**
- Modify: `src/provider/akshare_fetcher.py:270-283` (AkshareFetcher.__init__)
- Modify: `src/provider/akshare_fetcher.py` (新增方法，在 `_fetch_raw_data` 前)
- Test: `tests/test_provider/test_akshare_us.py` (新建)

- [ ] **Step 1: Create test file and write failing tests**

```bash
mkdir -p tests/test_provider
touch tests/test_provider/__init__.py
```

```python
# tests/test_provider/test_akshare_us.py
import unittest
from unittest.mock import patch, MagicMock
import pandas as pd


class TestToUsEmSymbol(unittest.TestCase):
    """测试美股东财代码格式转换"""

    def _make_fetcher(self):
        with patch("provider.akshare_fetcher.get_config") as mock_cfg:
            mock_cfg.return_value.enable_eastmoney_patch = False
            from provider.akshare_fetcher import AkshareFetcher
            return AkshareFetcher(sleep_min=0, sleep_max=0)

    def test_cache_hit(self):
        """缓存命中时直接返回，不调用 stock_us_spot_em"""
        fetcher = self._make_fetcher()
        fetcher._us_code_map = {"AAPL": "105.AAPL", "TSLA": "105.TSLA"}

        with patch("provider.akshare_fetcher.ak") as mock_ak:
            result = fetcher._to_us_em_symbol("AAPL")

        self.assertEqual(result, "105.AAPL")
        # stock_us_spot_em should NOT have been called
        mock_ak.stock_us_spot_em.assert_not_called()

    def test_cache_miss_builds_map(self):
        """缓存为空时调用 stock_us_spot_em 构建映射"""
        fetcher = self._make_fetcher()

        spot_df = pd.DataFrame({
            "代码": ["105.AAPL", "106.BAC", "105.TSLA"],
            "名称": ["苹果", "美国银行", "特斯拉"],
        })

        with patch("provider.akshare_fetcher.ak") as mock_ak:
            mock_ak.stock_us_spot_em.return_value = spot_df
            result = fetcher._to_us_em_symbol("AAPL")

        self.assertEqual(result, "105.AAPL")
        # 验证缓存已建立
        self.assertEqual(fetcher._us_code_map["TSLA"], "105.TSLA")

    def test_ticker_not_found_returns_none(self):
        """ticker 不在映射表中返回 None"""
        fetcher = self._make_fetcher()
        fetcher._us_code_map = {"AAPL": "105.AAPL"}

        result = fetcher._to_us_em_symbol("UNKNOWN")
        self.assertIsNone(result)

    def test_spot_em_failure_returns_none(self):
        """stock_us_spot_em 调用失败返回 None"""
        fetcher = self._make_fetcher()

        with patch("provider.akshare_fetcher.ak") as mock_ak:
            mock_ak.stock_us_spot_em.side_effect = Exception("network error")
            result = fetcher._to_us_em_symbol("AAPL")

        self.assertIsNone(result)

    def test_case_insensitive_input(self):
        """输入 ticker 不区分大小写"""
        fetcher = self._make_fetcher()
        fetcher._us_code_map = {"AAPL": "105.AAPL"}

        result = fetcher._to_us_em_symbol("aapl")
        self.assertEqual(result, "105.AAPL")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/gjh/code/AKI/massive-com && python -m pytest tests/test_provider/test_akshare_us.py::TestToUsEmSymbol -v`
Expected: FAIL — `_to_us_em_symbol` not defined, `_us_code_map` not initialized

- [ ] **Step 3: Add `_us_code_map` to `__init__` and implement `_to_us_em_symbol`**

In `src/provider/akshare_fetcher.py`, modify `AkshareFetcher.__init__` (line 270-283) to add cache init:

```python
def __init__(self, sleep_min: float = 2.0, sleep_max: float = 5.0):
    self.sleep_min = sleep_min
    self.sleep_max = sleep_max
    self._last_request_time: Optional[float] = None
    self._us_code_map: Optional[Dict[str, str]] = None  # 新增：美股代码映射缓存
    if get_config().enable_eastmoney_patch:
        eastmoney_patch()
```

Add new method after `_enforce_rate_limit` (after line 320), before `_fetch_raw_data`:

```python
def _to_us_em_symbol(self, ticker: str) -> Optional[str]:
    """
    将纯美股 ticker 转换为东财格式（如 AAPL → 105.AAPL）

    通过 ak.stock_us_spot_em() 获取完整代码映射并缓存。
    失败或找不到时返回 None，由调用方回退到 stock_us_daily。
    """
    ticker = ticker.strip().upper()

    if self._us_code_map is None:
        try:
            import akshare as ak
            spot_df = ak.stock_us_spot_em()
            self._us_code_map = {}
            if spot_df is not None and not spot_df.empty and '代码' in spot_df.columns:
                for _, row in spot_df.iterrows():
                    full_code = str(row['代码'])
                    # 代码格式：105.AAPL，取 . 后面的部分作为 key
                    parts = full_code.split('.', 1)
                    if len(parts) == 2:
                        self._us_code_map[parts[1].upper()] = full_code
            logger.info(f"[美股] 代码映射表构建完成，共 {len(self._us_code_map)} 个")
        except Exception as e:
            logger.warning(f"[美股] stock_us_spot_em 调用失败，将回退到 stock_us_daily: {e}")
            self._us_code_map = {}
            return None

    return self._us_code_map.get(ticker)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/gjh/code/AKI/massive-com && python -m pytest tests/test_provider/test_akshare_us.py::TestToUsEmSymbol -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_provider/__init__.py tests/test_provider/test_akshare_us.py src/provider/akshare_fetcher.py
git commit -m "feat(provider): add US stock code mapping _to_us_em_symbol to AkshareFetcher"
```

---

### Task 2: Provider 层 — 新增 `_fetch_us_data` 方法并修改 `_fetch_raw_data`

**Files:**
- Modify: `src/provider/akshare_fetcher.py:346-351` (_fetch_raw_data 美股分支)
- Modify: `src/provider/akshare_fetcher.py` (新增 _fetch_us_data 方法)
- Test: `tests/test_provider/test_akshare_us.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_provider/test_akshare_us.py`:

```python
class TestFetchUsData(unittest.TestCase):
    """测试美股数据获取及回退逻辑"""

    def _make_fetcher(self):
        with patch("provider.akshare_fetcher.get_config") as mock_cfg:
            mock_cfg.return_value.enable_eastmoney_patch = False
            from provider.akshare_fetcher import AkshareFetcher
            return AkshareFetcher(sleep_min=0, sleep_max=0)

    def _make_us_hist_df(self):
        """stock_us_hist 返回的中文列名 DataFrame"""
        return pd.DataFrame({
            "日期": ["2024-01-02", "2024-01-03"],
            "开盘": [185.0, 186.0],
            "收盘": [186.5, 185.5],
            "最高": [187.0, 187.5],
            "最低": [184.0, 184.5],
            "成交量": [50000000, 48000000],
            "成交额": [9300000000, 8900000000],
            "振幅": [1.62, 1.61],
            "涨跌幅": [0.81, -0.54],
            "涨跌额": [1.5, -1.0],
            "换手率": [0.32, 0.31],
        })

    def _make_us_daily_df(self):
        """stock_us_daily 返回的英文列名 DataFrame"""
        return pd.DataFrame({
            "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "open": [185.0, 186.0],
            "high": [187.0, 187.5],
            "low": [184.0, 184.5],
            "close": [186.5, 185.5],
            "volume": [50000000, 48000000],
        })

    def test_fetch_us_data_primary_success(self):
        """stock_us_hist 成功时直接返回"""
        fetcher = self._make_fetcher()
        fetcher._us_code_map = {"AAPL": "105.AAPL"}

        with patch("provider.akshare_fetcher.ak") as mock_ak:
            mock_ak.stock_us_hist.return_value = self._make_us_hist_df()
            df = fetcher._fetch_us_data("AAPL", "2024-01-01", "2024-01-31")

        self.assertEqual(len(df), 2)
        # stock_us_hist 应被调用
        mock_ak.stock_us_hist.assert_called_once()
        # stock_us_daily 不应被调用
        mock_ak.stock_us_daily.assert_not_called()

    def test_fetch_us_data_fallback_to_daily(self):
        """stock_us_hist 失败时回退到 stock_us_daily"""
        fetcher = self._make_fetcher()
        fetcher._us_code_map = {"AAPL": "105.AAPL"}

        with patch("provider.akshare_fetcher.ak") as mock_ak:
            mock_ak.stock_us_hist.side_effect = Exception("API error")
            mock_ak.stock_us_daily.return_value = self._make_us_daily_df()
            df = fetcher._fetch_us_data("AAPL", "2024-01-01", "2024-01-31")

        self.assertEqual(len(df), 2)
        mock_ak.stock_us_daily.assert_called_once()

    def test_fetch_us_data_fallback_when_no_em_symbol(self):
        """无法获取东财代码时直接回退到 stock_us_daily"""
        fetcher = self._make_fetcher()
        fetcher._us_code_map = {}  # AAPL 不在映射表中

        with patch("provider.akshare_fetcher.ak") as mock_ak:
            mock_ak.stock_us_daily.return_value = self._make_us_daily_df()
            df = fetcher._fetch_us_data("AAPL", "2024-01-01", "2024-01-31")

        self.assertEqual(len(df), 2)
        # stock_us_hist 不应被调用（因为没有东财代码）
        mock_ak.stock_us_hist.assert_not_called()

    def test_fetch_us_data_all_fail_raises(self):
        """两个源都失败时抛出 DataFetchError"""
        from provider.base import DataFetchError
        fetcher = self._make_fetcher()
        fetcher._us_code_map = {"AAPL": "105.AAPL"}

        with patch("provider.akshare_fetcher.ak") as mock_ak:
            mock_ak.stock_us_hist.side_effect = Exception("hist error")
            mock_ak.stock_us_daily.side_effect = Exception("daily error")

            with self.assertRaises(DataFetchError):
                fetcher._fetch_us_data("AAPL", "2024-01-01", "2024-01-31")

    def test_fetch_us_data_daily_filters_by_date(self):
        """回退到 stock_us_daily 时按日期范围过滤"""
        fetcher = self._make_fetcher()
        fetcher._us_code_map = {}

        daily_df = pd.DataFrame({
            "date": pd.to_datetime(["2023-12-29", "2024-01-02", "2024-02-01"]),
            "open": [180.0, 185.0, 190.0],
            "high": [181.0, 187.0, 191.0],
            "low": [179.0, 184.0, 189.0],
            "close": [180.5, 186.5, 190.5],
            "volume": [40000000, 50000000, 45000000],
        })

        with patch("provider.akshare_fetcher.ak") as mock_ak:
            mock_ak.stock_us_daily.return_value = daily_df
            df = fetcher._fetch_us_data("AAPL", "2024-01-01", "2024-01-31")

        self.assertEqual(len(df), 1)


class TestFetchRawDataUsStock(unittest.TestCase):
    """测试 _fetch_raw_data 的美股分支路由"""

    def _make_fetcher(self):
        with patch("provider.akshare_fetcher.get_config") as mock_cfg:
            mock_cfg.return_value.enable_eastmoney_patch = False
            from provider.akshare_fetcher import AkshareFetcher
            return AkshareFetcher(sleep_min=0, sleep_max=0)

    def test_us_code_routes_to_fetch_us_data(self):
        """美股代码应路由到 _fetch_us_data 而非抛异常"""
        fetcher = self._make_fetcher()
        fetcher._us_code_map = {"AAPL": "105.AAPL"}

        us_hist_df = pd.DataFrame({
            "日期": ["2024-01-02"],
            "开盘": [185.0],
            "收盘": [186.5],
            "最高": [187.0],
            "最低": [184.0],
            "成交量": [50000000],
            "成交额": [9300000000],
            "振幅": [1.62],
            "涨跌幅": [0.81],
            "涨跌额": [1.5],
            "换手率": [0.32],
        })

        with patch("provider.akshare_fetcher.ak") as mock_ak:
            mock_ak.stock_us_hist.return_value = us_hist_df
            df = fetcher._fetch_raw_data("AAPL", "2024-01-01", "2024-01-31")

        self.assertFalse(df.empty)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/gjh/code/AKI/massive-com && python -m pytest tests/test_provider/test_akshare_us.py::TestFetchUsData tests/test_provider/test_akshare_us.py::TestFetchRawDataUsStock -v`
Expected: FAIL — `_fetch_us_data` not defined; `_fetch_raw_data` still raises `DataFetchError` for US codes

- [ ] **Step 3: Implement `_fetch_us_data` and modify `_fetch_raw_data`**

In `src/provider/akshare_fetcher.py`, replace the US code branch in `_fetch_raw_data` (lines 346-351):

```python
# 原代码：
if _is_us_code(stock_code):
    raise DataFetchError(
        f"AkshareFetcher 不支持美股 {stock_code}，请使用 YfinanceFetcher 获取正确的复权价格"
    )

# 替换为：
if _is_us_code(stock_code):
    return self._fetch_us_data(stock_code, start_date, end_date)
```

Add `_fetch_us_data` method after `_to_us_em_symbol`:

```python
def _fetch_us_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """
    获取美股历史数据

    策略：
    1. 优先尝试东方财富接口 (ak.stock_us_hist) — 数据最全，含振幅/涨跌额/换手率
    2. 失败后回退新浪接口 (ak.stock_us_daily)
    """
    import akshare as ak

    ticker = stock_code.strip().upper()
    errors = []

    # 策略 1: stock_us_hist（需要东财格式代码）
    em_symbol = self._to_us_em_symbol(ticker)
    if em_symbol is not None:
        try:
            self._set_random_user_agent()
            self._enforce_rate_limit()

            logger.info(f"[API调用] ak.stock_us_hist(symbol={em_symbol}, period='daily', adjust='qfq')")
            df = ak.stock_us_hist(
                symbol=em_symbol,
                period="daily",
                start_date=start_date.replace('-', ''),
                end_date=end_date.replace('-', ''),
                adjust="qfq",
            )

            if df is not None and not df.empty:
                logger.info(f"[API返回] ak.stock_us_hist 成功: {len(df)} 行")
                return df
            else:
                logger.warning("[API返回] ak.stock_us_hist 返回空数据")
        except Exception as e:
            errors.append(f"stock_us_hist: {e}")
            logger.warning(f"[数据源] stock_us_hist 获取失败: {e}")

    # 策略 2: stock_us_daily 回退
    try:
        self._set_random_user_agent()
        self._enforce_rate_limit()

        logger.info(f"[API调用] ak.stock_us_daily(symbol={ticker}, adjust='qfq')")
        df = ak.stock_us_daily(symbol=ticker, adjust="qfq")

        if df is not None and not df.empty:
            # stock_us_daily 返回全量数据，按日期范围过滤
            df["date"] = pd.to_datetime(df["date"])
            start_dt = pd.to_datetime(start_date)
            end_dt = pd.to_datetime(end_date)
            df = df[(df["date"] >= start_dt) & (df["date"] <= end_dt)]

            if not df.empty:
                logger.info(f"[API返回] ak.stock_us_daily 成功（回退）: {len(df)} 行")
                return df

        logger.warning("[API返回] ak.stock_us_daily 返回空数据")
        errors.append("stock_us_daily: 空数据")
    except Exception as e:
        errors.append(f"stock_us_daily: {e}")
        logger.warning(f"[数据源] stock_us_daily 获取失败: {e}")

    raise DataFetchError(f"Akshare 所有渠道获取美股 {ticker} 失败: {'; '.join(errors)}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/gjh/code/AKI/massive-com && python -m pytest tests/test_provider/test_akshare_us.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/provider/akshare_fetcher.py tests/test_provider/test_akshare_us.py
git commit -m "feat(provider): add _fetch_us_data with stock_us_hist + stock_us_daily fallback"
```

---

### Task 3: Provider 层 — 扩展 `_normalize_data` 保留美股所有字段

**Files:**
- Modify: `src/provider/akshare_fetcher.py:744-779` (_normalize_data)
- Test: `tests/test_provider/test_akshare_us.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_provider/test_akshare_us.py`:

```python
class TestNormalizeDataUsStock(unittest.TestCase):
    """测试 _normalize_data 对美股数据的扩展字段保留"""

    def _make_fetcher(self):
        with patch("provider.akshare_fetcher.get_config") as mock_cfg:
            mock_cfg.return_value.enable_eastmoney_patch = False
            from provider.akshare_fetcher import AkshareFetcher
            return AkshareFetcher(sleep_min=0, sleep_max=0)

    def test_us_stock_keeps_all_fields(self):
        """美股数据应保留所有映射后的字段"""
        fetcher = self._make_fetcher()

        raw_df = pd.DataFrame({
            "日期": ["2024-01-02"],
            "开盘": [185.0],
            "收盘": [186.5],
            "最高": [187.0],
            "最低": [184.0],
            "成交量": [50000000],
            "成交额": [9300000000],
            "振幅": [1.62],
            "涨跌幅": [0.81],
            "涨跌额": [1.5],
            "换手率": [0.32],
        })

        result = fetcher._normalize_data(raw_df, "AAPL")

        # 标准字段
        self.assertIn("date", result.columns)
        self.assertIn("open", result.columns)
        self.assertIn("close", result.columns)
        self.assertIn("volume", result.columns)
        self.assertIn("amount", result.columns)
        self.assertIn("pct_chg", result.columns)
        # 美股扩展字段
        self.assertIn("amplitude", result.columns)
        self.assertIn("change", result.columns)
        self.assertIn("turnover_rate", result.columns)
        # code 字段
        self.assertIn("code", result.columns)
        self.assertEqual(result.iloc[0]["code"], "AAPL")

    def test_a_stock_still_filters_columns(self):
        """A 股数据仍然只保留 STANDARD_COLUMNS"""
        fetcher = self._make_fetcher()

        raw_df = pd.DataFrame({
            "日期": ["2024-01-02"],
            "开盘": [10.0],
            "收盘": [10.5],
            "最高": [11.0],
            "最低": [9.5],
            "成交量": [1000000],
            "成交额": [10500000],
            "振幅": [15.0],
            "涨跌幅": [5.0],
            "涨跌额": [0.5],
            "换手率": [1.2],
        })

        result = fetcher._normalize_data(raw_df, "600519")

        # A 股不应包含扩展字段
        self.assertNotIn("amplitude", result.columns)
        self.assertNotIn("change", result.columns)
        self.assertNotIn("turnover_rate", result.columns)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/gjh/code/AKI/massive-com && python -m pytest tests/test_provider/test_akshare_us.py::TestNormalizeDataUsStock -v`
Expected: FAIL — `amplitude`, `change`, `turnover_rate` columns missing for US stocks

- [ ] **Step 3: Modify `_normalize_data` to keep all fields for US stocks**

Replace `_normalize_data` in `src/provider/akshare_fetcher.py` (lines 744-779):

```python
def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
    """
    标准化 Akshare 数据

    Akshare 返回的列名（中文）：
    日期, 开盘, 收盘, 最高, 最低, 成交量, 成交额, 振幅, 涨跌幅, 涨跌额, 换手率

    需要映射到标准列名：
    date, open, high, low, close, volume, amount, pct_chg

    美股数据额外保留：amplitude, change, turnover_rate
    """
    df = df.copy()

    # 列名映射（Akshare 中文列名 -> 标准英文列名）
    column_mapping = {
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

    # 重命名列
    df = df.rename(columns=column_mapping)

    # 添加股票代码列
    df['code'] = stock_code

    if _is_us_code(stock_code):
        # 美股：保留所有已映射的列
        mapped_cols = ['code'] + [v for v in column_mapping.values() if v in df.columns]
        df = df[mapped_cols]
    else:
        # 非美股：只保留标准列
        keep_cols = ['code'] + STANDARD_COLUMNS
        existing_cols = [col for col in keep_cols if col in df.columns]
        df = df[existing_cols]

    return df
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/gjh/code/AKI/massive-com && python -m pytest tests/test_provider/test_akshare_us.py -v`
Expected: All tests PASS

- [ ] **Step 5: Run full provider test suite to check for regressions**

Run: `cd /Users/gjh/code/AKI/massive-com && python -m pytest tests/test_provider/ -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/provider/akshare_fetcher.py tests/test_provider/test_akshare_us.py
git commit -m "feat(provider): extend _normalize_data to keep all US stock fields"
```

---

### Task 4: Processor 层 — 重写 `AkshareSource.fetch_daily` 使用 `stock_us_hist`

**Files:**
- Modify: `src/processor/us_daily/sources/akshare_source.py`
- Modify: `tests/test_us_daily/test_sources/test_akshare_source.py`

- [ ] **Step 1: Write failing tests**

Replace `tests/test_us_daily/test_sources/test_akshare_source.py` with:

```python
import unittest
from unittest.mock import patch, MagicMock
import pandas as pd


class TestAkshareSource(unittest.TestCase):

    def _make_us_hist_df(self):
        """stock_us_hist 返回的中文列名 DataFrame"""
        return pd.DataFrame({
            "日期": ["2024-01-02", "2024-01-03"],
            "开盘": [185.0, 186.0],
            "收盘": [186.5, 185.5],
            "最高": [187.0, 187.5],
            "最低": [184.0, 184.5],
            "成交量": [50000000, 48000000],
            "成交额": [9300000000, 8900000000],
            "振幅": [1.62, 1.61],
            "涨跌幅": [0.81, -0.54],
            "涨跌额": [1.5, -1.0],
            "换手率": [0.32, 0.31],
        })

    def _make_us_daily_df(self):
        """stock_us_daily 返回的英文列名 DataFrame"""
        return pd.DataFrame({
            "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "open": [185.0, 186.0],
            "high": [187.0, 187.5],
            "low": [184.0, 184.5],
            "close": [186.5, 185.5],
            "volume": [50000000, 48000000],
        })

    def _make_spot_em_df(self):
        return pd.DataFrame({
            "代码": ["105.AAPL", "106.BAC"],
            "名称": ["苹果", "美国银行"],
        })

    def test_fetch_daily_uses_stock_us_hist_primary(self):
        """优先使用 stock_us_hist"""
        from processor.us_daily.sources.akshare_source import AkshareSource

        with patch("processor.us_daily.sources.akshare_source.ak") as mock_ak:
            mock_ak.stock_us_spot_em.return_value = self._make_spot_em_df()
            mock_ak.stock_us_hist.return_value = self._make_us_hist_df()
            source = AkshareSource(request_interval=0.0)
            result = source.fetch_daily("AAPL", "2024-01-01", "2024-01-31")

        mock_ak.stock_us_hist.assert_called_once()
        mock_ak.stock_us_daily.assert_not_called()
        self.assertEqual(len(result), 2)

    def test_fetch_daily_returns_all_fields(self):
        """返回所有字段，不仅限于 STANDARD_COLUMNS"""
        from processor.us_daily.sources.akshare_source import AkshareSource

        with patch("processor.us_daily.sources.akshare_source.ak") as mock_ak:
            mock_ak.stock_us_spot_em.return_value = self._make_spot_em_df()
            mock_ak.stock_us_hist.return_value = self._make_us_hist_df()
            source = AkshareSource(request_interval=0.0)
            result = source.fetch_daily("AAPL", "2024-01-01", "2024-01-31")

        # 标准字段
        for col in ["date", "open", "high", "low", "close", "volume"]:
            self.assertIn(col, result.columns)
        # 扩展字段
        for col in ["amount", "pct_chg", "amplitude", "change", "turnover_rate"]:
            self.assertIn(col, result.columns)

    def test_fetch_daily_fallback_to_stock_us_daily(self):
        """stock_us_hist 失败时回退到 stock_us_daily"""
        from processor.us_daily.sources.akshare_source import AkshareSource

        with patch("processor.us_daily.sources.akshare_source.ak") as mock_ak:
            mock_ak.stock_us_spot_em.return_value = self._make_spot_em_df()
            mock_ak.stock_us_hist.side_effect = Exception("API error")
            mock_ak.stock_us_daily.return_value = self._make_us_daily_df()
            source = AkshareSource(request_interval=0.0)
            result = source.fetch_daily("AAPL", "2024-01-01", "2024-01-31")

        self.assertEqual(len(result), 2)
        mock_ak.stock_us_daily.assert_called_once()

    def test_fetch_daily_fallback_filters_by_date(self):
        """回退到 stock_us_daily 时正确过滤日期"""
        from processor.us_daily.sources.akshare_source import AkshareSource

        daily_df = pd.DataFrame({
            "date": pd.to_datetime(["2023-12-31", "2024-01-02", "2024-02-01"]),
            "open": [70.0, 74.06, 80.0],
            "high": [71.0, 75.15, 81.0],
            "low": [69.0, 73.80, 79.0],
            "close": [70.5, 74.36, 80.5],
            "volume": [100000, 108872000, 90000],
        })

        with patch("processor.us_daily.sources.akshare_source.ak") as mock_ak:
            mock_ak.stock_us_spot_em.return_value = self._make_spot_em_df()
            mock_ak.stock_us_hist.side_effect = Exception("fail")
            mock_ak.stock_us_daily.return_value = daily_df
            source = AkshareSource(request_interval=0.0)
            result = source.fetch_daily("AAPL", "2024-01-01", "2024-01-31")

        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["date"], "2024-01-02")

    def test_fetch_daily_returns_empty_on_no_data(self):
        """无数据时返回空 DataFrame"""
        from processor.us_daily.sources.akshare_source import AkshareSource

        with patch("processor.us_daily.sources.akshare_source.ak") as mock_ak:
            mock_ak.stock_us_spot_em.return_value = self._make_spot_em_df()
            mock_ak.stock_us_hist.return_value = pd.DataFrame()
            mock_ak.stock_us_daily.return_value = pd.DataFrame()
            source = AkshareSource(request_interval=0.0)
            result = source.fetch_daily("AAPL", "2024-01-01", "2024-01-31")

        self.assertTrue(result.empty)

    def test_fetch_daily_case_insensitive_ticker(self):
        """ticker 输入不区分大小写"""
        from processor.us_daily.sources.akshare_source import AkshareSource

        with patch("processor.us_daily.sources.akshare_source.ak") as mock_ak:
            mock_ak.stock_us_spot_em.return_value = self._make_spot_em_df()
            mock_ak.stock_us_hist.return_value = self._make_us_hist_df()
            source = AkshareSource(request_interval=0.0)
            result = source.fetch_daily("aapl", "2024-01-01", "2024-01-31")

        self.assertEqual(len(result), 2)

    def test_code_map_cached(self):
        """stock_us_spot_em 只调用一次，第二次使用缓存"""
        from processor.us_daily.sources.akshare_source import AkshareSource

        with patch("processor.us_daily.sources.akshare_source.ak") as mock_ak:
            mock_ak.stock_us_spot_em.return_value = self._make_spot_em_df()
            mock_ak.stock_us_hist.return_value = self._make_us_hist_df()
            source = AkshareSource(request_interval=0.0)
            source.fetch_daily("AAPL", "2024-01-01", "2024-01-31")
            source.fetch_daily("AAPL", "2024-02-01", "2024-02-28")

        # stock_us_spot_em 只被调用 1 次
        self.assertEqual(mock_ak.stock_us_spot_em.call_count, 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/gjh/code/AKI/massive-com && python -m pytest tests/test_us_daily/test_sources/test_akshare_source.py -v`
Expected: FAIL — current `AkshareSource` uses `stock_us_daily` only, no `stock_us_hist`, no extended columns

- [ ] **Step 3: Rewrite `akshare_source.py`**

Replace `src/processor/us_daily/sources/akshare_source.py`:

```python
import logging
from typing import Dict, Optional

import pandas as pd

from processor.us_daily.sources.base import BaseSource, STANDARD_COLUMNS

logger = logging.getLogger("us_daily")

try:
    import akshare as ak
except ImportError:  # pragma: no cover
    ak = None  # type: ignore[assignment]

# 中文列名 → 英文列名映射
_COLUMN_MAPPING = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
    "涨跌幅": "pct_chg",
    "振幅": "amplitude",
    "涨跌额": "change",
    "换手率": "turnover_rate",
}


class AkshareSource(BaseSource):
    name = "akshare"

    def __init__(self, request_interval: float = 2.0):
        self.request_interval = request_interval
        self._us_code_map: Optional[Dict[str, str]] = None

    def _to_us_em_symbol(self, ticker: str) -> Optional[str]:
        """将纯 ticker 转换为东财格式（如 AAPL → 105.AAPL）"""
        ticker = ticker.strip().upper()

        if self._us_code_map is None:
            try:
                spot_df = ak.stock_us_spot_em()
                self._us_code_map = {}
                if spot_df is not None and not spot_df.empty and "代码" in spot_df.columns:
                    for _, row in spot_df.iterrows():
                        full_code = str(row["代码"])
                        parts = full_code.split(".", 1)
                        if len(parts) == 2:
                            self._us_code_map[parts[1].upper()] = full_code
                logger.debug(f"[akshare] US code map built: {len(self._us_code_map)} entries")
            except Exception as e:
                logger.warning(f"[akshare] stock_us_spot_em failed: {e}")
                self._us_code_map = {}
                return None

        return self._us_code_map.get(ticker)

    def fetch_daily(self, ticker: str, start_date: str, end_date: str) -> pd.DataFrame:
        if ak is None:
            raise ImportError("akshare is not installed")

        symbol = ticker.strip().upper()
        logger.debug(f"[akshare] fetching {symbol} {start_date}~{end_date}")

        # 策略 1: stock_us_hist（东财接口，字段最全）
        em_symbol = self._to_us_em_symbol(symbol)
        if em_symbol is not None:
            try:
                df = ak.stock_us_hist(
                    symbol=em_symbol,
                    period="daily",
                    start_date=start_date.replace("-", ""),
                    end_date=end_date.replace("-", ""),
                    adjust="qfq",
                )
                if df is not None and not df.empty:
                    # 中文列名转英文
                    df = df.rename(columns=_COLUMN_MAPPING)
                    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
                    logger.debug(f"[akshare] stock_us_hist success: {len(df)} rows")
                    return df.reset_index(drop=True)
            except Exception as e:
                logger.warning(f"[akshare] stock_us_hist failed for {symbol}: {e}")

        # 策略 2: stock_us_daily 回退
        try:
            df = ak.stock_us_daily(symbol=symbol, adjust="qfq")

            if df is None or df.empty:
                return pd.DataFrame(columns=STANDARD_COLUMNS)

            df["date"] = pd.to_datetime(df["date"])
            start_dt = pd.to_datetime(start_date)
            end_dt = pd.to_datetime(end_date)
            df = df[(df["date"] >= start_dt) & (df["date"] <= end_dt)]

            if df.empty:
                return pd.DataFrame(columns=STANDARD_COLUMNS)

            df["date"] = df["date"].dt.strftime("%Y-%m-%d")
            return df.reset_index(drop=True)
        except Exception as e:
            logger.warning(f"[akshare] stock_us_daily also failed for {symbol}: {e}")
            return pd.DataFrame(columns=STANDARD_COLUMNS)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/gjh/code/AKI/massive-com && python -m pytest tests/test_us_daily/test_sources/test_akshare_source.py -v`
Expected: All tests PASS

- [ ] **Step 5: Run full us_daily test suite to check for regressions**

Run: `cd /Users/gjh/code/AKI/massive-com && python -m pytest tests/test_us_daily/ -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/processor/us_daily/sources/akshare_source.py tests/test_us_daily/test_sources/test_akshare_source.py
git commit -m "feat(processor): rewrite AkshareSource to use stock_us_hist with fallback"
```

---

### Task 5: 端到端验证 — 更新 `_fetch_raw_data` docstring 并运行全量测试

**Files:**
- Modify: `src/provider/akshare_fetcher.py:328-343` (docstring)

- [ ] **Step 1: Update `_fetch_raw_data` docstring**

In `src/provider/akshare_fetcher.py`, update the docstring of `_fetch_raw_data` (lines 329-343):

```python
def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """
    从 Akshare 获取原始数据

    根据代码类型自动选择 API：
    - 美股：优先 ak.stock_us_hist()，回退 ak.stock_us_daily()
    - 港股：使用 ak.stock_hk_hist()
    - ETF 基金：使用 ak.fund_etf_hist_em()
    - 普通 A 股：使用 ak.stock_zh_a_hist()

    流程：
    1. 判断代码类型（美股/港股/ETF/A股）
    2. 设置随机 User-Agent
    3. 执行速率限制（随机休眠）
    4. 调用对应的 akshare API
    5. 处理返回数据
    """
```

- [ ] **Step 2: Run full test suite**

Run: `cd /Users/gjh/code/AKI/massive-com && python -m pytest tests/ -v`
Expected: All tests PASS, no regressions

- [ ] **Step 3: Commit**

```bash
git add src/provider/akshare_fetcher.py
git commit -m "docs(provider): update _fetch_raw_data docstring for US stock support"
```
