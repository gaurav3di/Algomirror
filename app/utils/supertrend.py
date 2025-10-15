"""
Supertrend Indicator Module
Calculates Supertrend using talib for ATR calculation
"""
import numpy as np
from numba import njit
import talib
import logging

logger = logging.getLogger(__name__)


def get_basic_bands(med_price, atr, multiplier):
    """
    Calculate basic upper and lower bands

    Args:
        med_price: Median price array
        atr: Average True Range array
        multiplier: ATR multiplier

    Returns:
        Tuple of (upper_band, lower_band)
    """
    matr = multiplier * atr
    upper = med_price + matr
    lower = med_price - matr
    return upper, lower


@njit
def get_final_bands_nb(close, upper, lower):
    """
    Calculate final Supertrend bands and direction
    Optimized with Numba for performance

    Args:
        close: Close price array
        upper: Upper band array
        lower: Lower band array

    Returns:
        Tuple of (trend, direction, long, short)
    """
    trend = np.full(close.shape, np.nan)
    dir_ = np.full(close.shape, 1)
    long = np.full(close.shape, np.nan)
    short = np.full(close.shape, np.nan)

    for i in range(1, close.shape[0]):
        if close[i] > upper[i - 1]:
            dir_[i] = 1
        elif close[i] < lower[i - 1]:
            dir_[i] = -1
        else:
            dir_[i] = dir_[i - 1]
            if dir_[i] > 0 and lower[i] < lower[i - 1]:
                lower[i] = lower[i - 1]
            if dir_[i] < 0 and upper[i] > upper[i - 1]:
                upper[i] = upper[i - 1]

        if dir_[i] > 0:
            trend[i] = long[i] = lower[i]
        else:
            trend[i] = short[i] = upper[i]

    return trend, dir_, long, short


def calculate_supertrend(high, low, close, period=7, multiplier=3):
    """
    Calculate Supertrend indicator using talib for ATR

    Args:
        high: High price array (numpy array or pandas Series)
        low: Low price array (numpy array or pandas Series)
        close: Close price array (numpy array or pandas Series)
        period: ATR period (default: 7)
        multiplier: ATR multiplier (default: 3)

    Returns:
        Tuple of (trend, direction, long, short)
        - trend: Supertrend line values
        - direction: 1 for bullish, -1 for bearish
        - long: Long (support) line
        - short: Short (resistance) line
    """
    try:
        # Convert to numpy arrays if needed
        if hasattr(high, 'values'):
            high = high.values
        if hasattr(low, 'values'):
            low = low.values
        if hasattr(close, 'values'):
            close = close.values

        # Calculate median price using talib
        avg_price = talib.MEDPRICE(high, low)

        # Calculate ATR using talib
        atr = talib.ATR(high, low, close, period)

        # Get basic bands
        upper, lower = get_basic_bands(avg_price, atr, multiplier)

        # Calculate final bands with direction
        trend, direction, long, short = get_final_bands_nb(close, upper, lower)

        logger.debug(f"Supertrend calculated: period={period}, multiplier={multiplier}")

        return trend, direction, long, short

    except Exception as e:
        logger.error(f"Error calculating Supertrend: {e}", exc_info=True)
        # Return NaN arrays on error
        nan_array = np.full(close.shape, np.nan)
        return nan_array, nan_array, nan_array, nan_array


def get_supertrend_signal(direction):
    """
    Get current Supertrend signal

    Args:
        direction: Direction array from calculate_supertrend

    Returns:
        String: 'BUY', 'SELL', or 'NEUTRAL'
    """
    if len(direction) == 0:
        return 'NEUTRAL'

    current_dir = direction[-1]

    if np.isnan(current_dir):
        return 'NEUTRAL'
    elif current_dir > 0:
        return 'BUY'
    else:
        return 'SELL'


def calculate_spread_supertrend(leg_prices_dict, high_col='high', low_col='low', close_col='close',
                                period=7, multiplier=3):
    """
    Calculate Supertrend for a combined spread of multiple legs

    Args:
        leg_prices_dict: Dict of {leg_name: DataFrame} with OHLC data
        high_col: Column name for high price
        low_col: Column name for low price
        close_col: Column name for close price
        period: ATR period
        multiplier: ATR multiplier

    Returns:
        Dict with spread OHLC and Supertrend data
    """
    try:
        if not leg_prices_dict:
            logger.error("No leg prices provided")
            return None

        # Calculate combined spread
        # For now, simple sum of close prices (can be customized based on strategy)
        combined_high = None
        combined_low = None
        combined_close = None

        for leg_name, df in leg_prices_dict.items():
            if combined_close is None:
                combined_high = df[high_col].copy()
                combined_low = df[low_col].copy()
                combined_close = df[close_col].copy()
            else:
                combined_high += df[high_col]
                combined_low += df[low_col]
                combined_close += df[close_col]

        # Calculate Supertrend on combined spread
        trend, direction, long, short = calculate_supertrend(
            combined_high.values,
            combined_low.values,
            combined_close.values,
            period=period,
            multiplier=multiplier
        )

        return {
            'high': combined_high,
            'low': combined_low,
            'close': combined_close,
            'supertrend': trend,
            'direction': direction,
            'long': long,
            'short': short,
            'signal': get_supertrend_signal(direction)
        }

    except Exception as e:
        logger.error(f"Error calculating spread Supertrend: {e}", exc_info=True)
        return None
