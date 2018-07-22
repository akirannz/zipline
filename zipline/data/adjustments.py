from collections import namedtuple
from errno import ENOENT
from os import remove

from logbook import Logger
import numpy as np
import pandas as pd
from pandas import Timestamp
import six
import sqlite3

from zipline.data.bar_reader import NoDataOnDate
from zipline.utils.input_validation import preprocess
from zipline.utils.numpy_utils import (
    float64_dtype,
    int64_dtype,
    uint32_dtype,
)
from zipline.utils.sqlite_utils import group_into_chunks, coerce_string_to_conn
from ._adjustments import load_adjustments_from_sqlite

log = Logger(__name__)


SQLITE_ADJUSTMENT_TABLENAMES = frozenset(['splits', 'dividends', 'mergers'])

UNPAID_QUERY_TEMPLATE = """
SELECT sid, amount, pay_date from dividend_payouts
WHERE ex_date=? AND sid IN ({0})
"""

Dividend = namedtuple('Dividend', ['asset', 'amount', 'pay_date'])

UNPAID_STOCK_DIVIDEND_QUERY_TEMPLATE = """
SELECT sid, payment_sid, ratio, pay_date from stock_dividend_payouts
WHERE ex_date=? AND sid IN ({0})
"""

StockDividend = namedtuple(
    'StockDividend',
    ['asset', 'payment_asset', 'ratio', 'pay_date'],
)


SQLITE_ADJUSTMENT_COLUMN_DTYPES = {
    'effective_date': int64_dtype,
    'ratio': float64_dtype,
    'sid': int64_dtype,
}


SQLITE_DIVIDEND_PAYOUT_COLUMN_DTYPES = {
    'sid': int64_dtype,
    'ex_date': int64_dtype,
    'declared_date': int64_dtype,
    'record_date': int64_dtype,
    'pay_date': int64_dtype,
    'amount': float,
}


SQLITE_STOCK_DIVIDEND_PAYOUT_COLUMN_DTYPES = {
    'sid': int64_dtype,
    'ex_date': int64_dtype,
    'declared_date': int64_dtype,
    'record_date': int64_dtype,
    'pay_date': int64_dtype,
    'payment_sid': int64_dtype,
    'ratio': float,
}


class SQLiteAdjustmentReader(object):
    """
    Loads adjustments based on corporate actions from a SQLite database.

    Expects data written in the format output by `SQLiteAdjustmentWriter`.

    Parameters
    ----------
    conn : str or sqlite3.Connection
        Connection from which to load data.

    See Also
    --------
    :class:`zipline.data.us_equity_pricing.SQLiteAdjustmentWriter`
    """

    @preprocess(conn=coerce_string_to_conn(require_exists=True))
    def __init__(self, conn):
        self.conn = conn

        # Given the tables in the adjustments.db file, dict which knows which
        # col names contain dates that have been coerced into ints.
        self._datetime_int_cols = {
            'dividend_payouts': ('declared_date', 'ex_date', 'pay_date',
                                 'record_date'),
            'dividends': ('effective_date',),
            'mergers': ('effective_date',),
            'splits': ('effective_date',),
            'stock_dividend_payouts': ('declared_date', 'ex_date', 'pay_date',
                                       'record_date')
        }

    def load_adjustments(self, columns, dates, assets):
        return load_adjustments_from_sqlite(
            self.conn,
            list(columns),
            dates,
            assets,
        )

    def get_adjustments_for_sid(self, table_name, sid):
        t = (sid,)
        c = self.conn.cursor()
        adjustments_for_sid = c.execute(
            "SELECT effective_date, ratio FROM %s WHERE sid = ?" %
            table_name, t).fetchall()
        c.close()

        return [[Timestamp(adjustment[0], unit='s', tz='UTC'), adjustment[1]]
                for adjustment in
                adjustments_for_sid]

    def get_dividends_with_ex_date(self, assets, date, asset_finder):
        seconds = date.value / int(1e9)
        c = self.conn.cursor()

        divs = []
        for chunk in group_into_chunks(assets):
            query = UNPAID_QUERY_TEMPLATE.format(
                ",".join(['?' for _ in chunk]))
            t = (seconds,) + tuple(map(lambda x: int(x), chunk))

            c.execute(query, t)

            rows = c.fetchall()
            for row in rows:
                div = Dividend(
                    asset_finder.retrieve_asset(row[0]),
                    row[1], Timestamp(row[2], unit='s', tz='UTC'))
                divs.append(div)
        c.close()

        return divs

    def get_stock_dividends_with_ex_date(self, assets, date, asset_finder):
        seconds = date.value / int(1e9)
        c = self.conn.cursor()

        stock_divs = []
        for chunk in group_into_chunks(assets):
            query = UNPAID_STOCK_DIVIDEND_QUERY_TEMPLATE.format(
                ",".join(['?' for _ in chunk]))
            t = (seconds,) + tuple(map(lambda x: int(x), chunk))

            c.execute(query, t)

            rows = c.fetchall()

            for row in rows:
                stock_div = StockDividend(
                    asset_finder.retrieve_asset(row[0]),    # asset
                    asset_finder.retrieve_asset(row[1]),    # payment_asset
                    row[2],
                    Timestamp(row[3], unit='s', tz='UTC'))
                stock_divs.append(stock_div)
        c.close()

        return stock_divs

    def unpack_db_to_component_dfs(self, convert_dates=False):
        """Returns the set of known tables in the adjustments file in DataFrame
        form.

        Parameters
        ----------
        convert_dates : bool, optional
            By default, dates are returned in seconds since EPOCH. If
            convert_dates is True, all ints in date columns will be converted
            to datetimes.

        Returns
        -------
        dfs : dict{str->DataFrame}
            Dictionary which maps table name to the corresponding DataFrame
            version of the table, where all date columns have been coerced back
            from int to datetime.
        """

        def _get_df_from_table(table_name, date_cols):

            # Dates are stored in second resolution as ints in adj.db tables.
            # Need to specifically convert them as UTC, not local time.
            kwargs = (
                {'parse_dates': {col: {'unit': 's', 'utc': True}
                                 for col in date_cols}
                 }
                if convert_dates
                else {}
            )

            return pd.read_sql(
                'select * from "{}"'.format(table_name),
                self.conn,
                index_col='index',
                **kwargs
            ).rename_axis(None)

        return {
            t_name: _get_df_from_table(
                t_name,
                date_cols
            )
            for t_name, date_cols in self._datetime_int_cols.items()
        }


class SQLiteAdjustmentWriter(object):
    """
    Writer for data to be read by SQLiteAdjustmentReader

    Parameters
    ----------
    conn_or_path : str or sqlite3.Connection
        A handle to the target sqlite database.
    equity_daily_bar_reader : BcolzDailyBarReader
        Daily bar reader to use for dividend writes.
    overwrite : bool, optional, default=False
        If True and conn_or_path is a string, remove any existing files at the
        given path before connecting.

    See Also
    --------
    zipline.data.us_equity_pricing.SQLiteAdjustmentReader
    """

    def __init__(self,
                 conn_or_path,
                 equity_daily_bar_reader,
                 calendar,
                 overwrite=False):
        if isinstance(conn_or_path, sqlite3.Connection):
            self.conn = conn_or_path
        elif isinstance(conn_or_path, six.string_types):
            if overwrite:
                try:
                    remove(conn_or_path)
                except OSError as e:
                    if e.errno != ENOENT:
                        raise
            self.conn = sqlite3.connect(conn_or_path)
            self.uri = conn_or_path
        else:
            raise TypeError("Unknown connection type %s" % type(conn_or_path))

        self._equity_daily_bar_reader = equity_daily_bar_reader
        self._calendar = calendar

    def _write(self, tablename, expected_dtypes, frame):
        if frame is None or frame.empty:
            # keeping the dtypes correct for empty frames is not easy
            frame = pd.DataFrame(
                np.array([], dtype=list(expected_dtypes.items())),
            )
        else:
            if frozenset(frame.columns) != frozenset(expected_dtypes):
                raise ValueError(
                    "Unexpected frame columns:\n"
                    "Expected Columns: %s\n"
                    "Received Columns: %s" % (
                        set(expected_dtypes),
                        frame.columns.tolist(),
                    )
                )

            actual_dtypes = frame.dtypes
            for colname, expected in six.iteritems(expected_dtypes):
                actual = actual_dtypes[colname]
                if not np.issubdtype(actual, expected):
                    raise TypeError(
                        "Expected data of type {expected} for column"
                        " '{colname}', but got '{actual}'.".format(
                            expected=expected,
                            colname=colname,
                            actual=actual,
                        ),
                    )

        frame.to_sql(
            tablename,
            self.conn,
            if_exists='append',
            chunksize=50000,
        )

    def write_frame(self, tablename, frame):
        if tablename not in SQLITE_ADJUSTMENT_TABLENAMES:
            raise ValueError(
                "Adjustment table %s not in %s" % (
                    tablename,
                    SQLITE_ADJUSTMENT_TABLENAMES,
                )
            )
        if not (frame is None or frame.empty):
            frame = frame.copy()
            frame['effective_date'] = frame['effective_date'].values.astype(
                'datetime64[s]',
            ).astype('int64')
        return self._write(
            tablename,
            SQLITE_ADJUSTMENT_COLUMN_DTYPES,
            frame,
        )

    def write_dividend_payouts(self, frame):
        """
        Write dividend payout data to SQLite table `dividend_payouts`.
        """
        return self._write(
            'dividend_payouts',
            SQLITE_DIVIDEND_PAYOUT_COLUMN_DTYPES,
            frame,
        )

    def write_stock_dividend_payouts(self, frame):
        return self._write(
            'stock_dividend_payouts',
            SQLITE_STOCK_DIVIDEND_PAYOUT_COLUMN_DTYPES,
            frame,
        )

    def calc_dividend_ratios(self, dividends):
        """
        Calculate the ratios to apply to equities when looking back at pricing
        history so that the price is smoothed over the ex_date, when the market
        adjusts to the change in equity value due to upcoming dividend.

        Returns
        -------
        DataFrame
            A frame in the same format as splits and mergers, with keys
            - sid, the id of the equity
            - effective_date, the date in seconds on which to apply the ratio.
            - ratio, the ratio to apply to backwards looking pricing data.
        """
        if dividends is None or dividends.empty:
            return pd.DataFrame(np.array(
                [],
                dtype=[
                    ('sid', uint32_dtype),
                    ('effective_date', uint32_dtype),
                    ('ratio', float64_dtype),
                ],
            ))
        ex_dates = dividends.ex_date.values

        sids = dividends.sid.values
        amounts = dividends.amount.values

        ratios = np.full(len(amounts), np.nan)

        equity_daily_bar_reader = self._equity_daily_bar_reader

        effective_dates = np.full(len(amounts), -1, dtype=int64_dtype)

        calendar = self._calendar

        # Calculate locs against a tz-naive cal, as the ex_dates are tz-
        # naive.
        #
        # TODO: A better approach here would be to localize ex_date to
        # the tz of the calendar, but currently get_indexer does not
        # preserve tz of the target when method='bfill', which throws
        # off the comparison.
        tz_naive_calendar = calendar.tz_localize(None)
        day_locs = tz_naive_calendar.get_indexer(ex_dates, method='bfill')

        isnull = np.isnull

        for i, amount in enumerate(amounts):
            sid = sids[i]
            ex_date = ex_dates[i]
            day_loc = day_locs[i]

            prev_close_date = calendar[day_loc - 1]

            try:
                prev_close = equity_daily_bar_reader.get_value(
                    sid, prev_close_date, 'close')
                if not isnull(prev_close):
                    ratio = 1.0 - amount / prev_close
                    ratios[i] = ratio
                    # only assign effective_date when data is found
                    effective_dates[i] = ex_date
            except NoDataOnDate:
                log.warn("Couldn't compute ratio for dividend %s" % {
                    'sid': sid,
                    'ex_date': ex_date,
                    'amount': amount,
                })
                continue

        # Create a mask to filter out indices in the effective_date, sid, and
        # ratio vectors for which a ratio was not calculable.
        effective_mask = effective_dates != -1
        effective_dates = effective_dates[effective_mask]
        effective_dates = effective_dates.astype('datetime64[ns]').\
            astype('datetime64[s]').astype(uint32_dtype)
        sids = sids[effective_mask]
        ratios = ratios[effective_mask]

        return pd.DataFrame({
            'sid': sids,
            'effective_date': effective_dates,
            'ratio': ratios,
        })

    def _write_dividends(self, dividends):
        if dividends is None:
            dividend_payouts = None
        else:
            dividend_payouts = dividends.copy()
            dividend_payouts['ex_date'] = dividend_payouts['ex_date'].values.\
                astype('datetime64[s]').astype(int64_dtype)
            dividend_payouts['record_date'] = \
                dividend_payouts['record_date'].values.\
                astype('datetime64[s]').astype(int64_dtype)
            dividend_payouts['declared_date'] = \
                dividend_payouts['declared_date'].values.\
                astype('datetime64[s]').astype(int64_dtype)
            dividend_payouts['pay_date'] = \
                dividend_payouts['pay_date'].values.astype('datetime64[s]').\
                astype(int64_dtype)

        self.write_dividend_payouts(dividend_payouts)

    def _write_stock_dividends(self, stock_dividends):
        if stock_dividends is None:
            stock_dividend_payouts = None
        else:
            stock_dividend_payouts = stock_dividends.copy()
            stock_dividend_payouts['ex_date'] = \
                stock_dividend_payouts['ex_date'].values.\
                astype('datetime64[s]').astype(int64_dtype)
            stock_dividend_payouts['record_date'] = \
                stock_dividend_payouts['record_date'].values.\
                astype('datetime64[s]').astype(int64_dtype)
            stock_dividend_payouts['declared_date'] = \
                stock_dividend_payouts['declared_date'].\
                values.astype('datetime64[s]').astype(int64_dtype)
            stock_dividend_payouts['pay_date'] = \
                stock_dividend_payouts['pay_date'].\
                values.astype('datetime64[s]').astype(int64_dtype)
        self.write_stock_dividend_payouts(stock_dividend_payouts)

    def write_dividend_data(self, dividends, stock_dividends=None):
        """
        Write both dividend payouts and the derived price adjustment ratios.
        """

        # First write the dividend payouts.
        self._write_dividends(dividends)
        self._write_stock_dividends(stock_dividends)

        # Second from the dividend payouts, calculate ratios.
        dividend_ratios = self.calc_dividend_ratios(dividends)
        self.write_frame('dividends', dividend_ratios)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def write(self,
              splits=None,
              mergers=None,
              dividends=None,
              stock_dividends=None):
        """
        Writes data to a SQLite file to be read by SQLiteAdjustmentReader.

        Parameters
        ----------
        splits : pandas.DataFrame, optional
            Dataframe containing split data. The format of this dataframe is:
              effective_date : int
                  The date, represented as seconds since Unix epoch, on which
                  the adjustment should be applied.
              ratio : float
                  A value to apply to all data earlier than the effective date.
                  For open, high, low, and close those values are multiplied by
                  the ratio. Volume is divided by this value.
              sid : int
                  The asset id associated with this adjustment.
        mergers : pandas.DataFrame, optional
            DataFrame containing merger data. The format of this dataframe is:
              effective_date : int
                  The date, represented as seconds since Unix epoch, on which
                  the adjustment should be applied.
              ratio : float
                  A value to apply to all data earlier than the effective date.
                  For open, high, low, and close those values are multiplied by
                  the ratio. Volume is unaffected.
              sid : int
                  The asset id associated with this adjustment.
        dividends : pandas.DataFrame, optional
            DataFrame containing dividend data. The format of the dataframe is:
              sid : int
                  The asset id associated with this adjustment.
              ex_date : datetime64
                  The date on which an equity must be held to be eligible to
                  receive payment.
              declared_date : datetime64
                  The date on which the dividend is announced to the public.
              pay_date : datetime64
                  The date on which the dividend is distributed.
              record_date : datetime64
                  The date on which the stock ownership is checked to determine
                  distribution of dividends.
              amount : float
                  The cash amount paid for each share.

            Dividend ratios are calculated as:
            ``1.0 - (dividend_value / "close on day prior to ex_date")``
        stock_dividends : pandas.DataFrame, optional
            DataFrame containing stock dividend data. The format of the
            dataframe is:
              sid : int
                  The asset id associated with this adjustment.
              ex_date : datetime64
                  The date on which an equity must be held to be eligible to
                  receive payment.
              declared_date : datetime64
                  The date on which the dividend is announced to the public.
              pay_date : datetime64
                  The date on which the dividend is distributed.
              record_date : datetime64
                  The date on which the stock ownership is checked to determine
                  distribution of dividends.
              payment_sid : int
                  The asset id of the shares that should be paid instead of
                  cash.
              ratio : float
                  The ratio of currently held shares in the held sid that
                  should be paid with new shares of the payment_sid.

        See Also
        --------
        zipline.data.us_equity_pricing.SQLiteAdjustmentReader
        """
        self.write_frame('splits', splits)
        self.write_frame('mergers', mergers)
        self.write_dividend_data(dividends, stock_dividends)
        self.conn.execute(
            "CREATE INDEX splits_sids "
            "ON splits(sid)"
        )
        self.conn.execute(
            "CREATE INDEX splits_effective_date "
            "ON splits(effective_date)"
        )
        self.conn.execute(
            "CREATE INDEX mergers_sids "
            "ON mergers(sid)"
        )
        self.conn.execute(
            "CREATE INDEX mergers_effective_date "
            "ON mergers(effective_date)"
        )
        self.conn.execute(
            "CREATE INDEX dividends_sid "
            "ON dividends(sid)"
        )
        self.conn.execute(
            "CREATE INDEX dividends_effective_date "
            "ON dividends(effective_date)"
        )
        self.conn.execute(
            "CREATE INDEX dividend_payouts_sid "
            "ON dividend_payouts(sid)"
        )
        self.conn.execute(
            "CREATE INDEX dividends_payouts_ex_date "
            "ON dividend_payouts(ex_date)"
        )
        self.conn.execute(
            "CREATE INDEX stock_dividend_payouts_sid "
            "ON stock_dividend_payouts(sid)"
        )
        self.conn.execute(
            "CREATE INDEX stock_dividends_payouts_ex_date "
            "ON stock_dividend_payouts(ex_date)"
        )

    def close(self):
        self.conn.close()
