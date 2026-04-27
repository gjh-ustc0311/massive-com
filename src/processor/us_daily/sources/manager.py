import logging
import time
from typing import List, Tuple

import pandas as pd

from processor.us_daily.sources.base import BaseSource

logger = logging.getLogger("us_daily")


class FetchError(Exception):
    """Raised when all data sources fail."""
    pass


MAX_RETRIES = 3


class SourceManager:
    def __init__(self, sources: List[BaseSource]):
        self.sources = sources

    def fetch_daily(
        self, ticker: str, start_date: str, end_date: str
    ) -> Tuple[pd.DataFrame, str]:
        """Try each source in priority order, retrying up to MAX_RETRIES times.

        Return (df, source_name). Raises FetchError if all sources fail.
        """
        errors = []
        for source in self.sources:
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    time.sleep(source.request_interval)
                    df = source.fetch_daily(ticker, start_date, end_date)
                    if df is not None and not df.empty:
                        return df, source.name
                    logger.debug(
                        f"{source.name} returned empty data for {ticker} "
                        f"(attempt {attempt}/{MAX_RETRIES})"
                    )
                except Exception as e:
                    logger.warning(
                        f"{source.name} failed for {ticker} "
                        f"(attempt {attempt}/{MAX_RETRIES}): {e}"
                    )
                    if attempt == MAX_RETRIES:
                        errors.append(f"{source.name}: {e}")
        raise FetchError(
            f"All sources failed for {ticker}: {'; '.join(errors)}"
        )
