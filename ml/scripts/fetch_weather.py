import datetime as dt
import sys

from mkl import db
from mkl.features import external

sys.stdout.reconfigure(encoding="utf-8")

con = db.connect()
db.attach_events(con)
start, last_event_day = con.execute("SELECT min(day), max(day) FROM ev").fetchone()
con.close()
# Архив погоды публикуется с задержкой. Более свежие строки телеметрии
# остаются без погодных признаков до следующего обновления кэша.
end = min(last_event_day, dt.date.today() - dt.timedelta(days=2))
df = external.fetch_moscow_weather(start, end)
if df is None:
    print("погода недоступна — пайплайн продолжит работу на календарных фичах")
else:
    print(df.shape)
    print(df.head(3))
    print(df.tail(3))
