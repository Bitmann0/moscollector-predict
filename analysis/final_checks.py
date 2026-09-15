import pathlib,sys,json
OUT=pathlib.Path(__file__).resolve().parent
sys.path.insert(0,str(OUT/'deps'))
import duckdb
con=duckdb.connect(str(OUT/'audit.duckdb'))
con.execute("SET memory_limit='6GB'")
con.execute('SET threads=4')
def rows(sql):
 q=con.execute(sql); return [dict(zip([x[0] for x in q.description],r)) for r in q.fetchall()]
r={}
r['multi_state_examples']=rows('''SELECT sensor_type,channel,ts,list(DISTINCT sensor_value ORDER BY sensor_value) states
 FROM operational_states WHERE sensor_type='Состояние насоса' GROUP BY 1,2,3 HAVING count(DISTINCT sensor_value)>2 ORDER BY ts DESC LIMIT 12''')
r['multi_state_combinations']=rows('''SELECT sensor_type,states,count(*) n FROM (SELECT sensor_type,channel,ts,
 string_agg(DISTINCT sensor_value,' / ' ORDER BY sensor_value) states FROM operational_states GROUP BY 1,2,3 HAVING count(DISTINCT sensor_value)>1)
 GROUP BY 1,2 ORDER BY n DESC LIMIT 25''')
r['temperature_extremes']=rows('''SELECT channel,sensor_value,sum(n) n FROM all_values v JOIN channels c ON channel=c."ид_канала_данных"
 WHERE c."тип_датчика"='Датчик температуры' AND (try_cast(sensor_value AS DOUBLE)<-80 OR try_cast(sensor_value AS DOUBLE)>150)
 GROUP BY 1,2 ORDER BY n DESC LIMIT 25''')
r['daily_type_concentration']=rows('''SELECT year(ts) yr,e.channel,c."тип_датчика" sensor_type,count(*) n,count(*) FILTER(WHERE alarm) alarms
 FROM all_events e JOIN channels c ON e.channel=c."ид_канала_данных" WHERE alarm GROUP BY 1,2,3 ORDER BY alarms DESC LIMIT 20''')
r['raw_minute_peak']=rows("SELECT date_trunc('minute',ts) minute_start,count(*) n FROM events_2025 WHERE event_date='2025-11-06' GROUP BY 1 ORDER BY n DESC LIMIT 5")
(OUT/'final_checks.json').write_text(json.dumps(r,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
print('Context checks complete',flush=True)
# The all-history ID check is partitioned to bound memory use; run check_global_ids.py next.
con.close()
