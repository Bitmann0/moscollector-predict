import datetime as dt
import sys

from mkl.features import external

sys.stdout.reconfigure(encoding="utf-8")

df = external.fetch_moscow_weather(dt.date(2019, 1, 1), dt.date(2026, 6, 30))
if df is None:
    print("погода недоступна — пайплайн продолжит работу на календарных фичах")
else:
    print(df.shape)
    print(df.head(3))
    print(df.tail(3))
