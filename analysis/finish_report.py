import pathlib,json,sys
OUT=pathlib.Path(__file__).resolve().parent
profiles=json.loads((OUT/'annual_profiles.json').read_text(encoding='utf-8'))
def fmt(n): return f'{n:,}'.replace(',',' ')
lines=['| Год | Строк | Каналов | Дней с данными | Тревожных строк | Строк без связи с каталогом |',
       '|---|---:|---:|---:|---:|---:|']
for p in profiles:
    s=p['summary']
    lines.append('| '+p['year']+(' (янв.–июнь)' if p['year']=='2026' else '')+' | '+' | '.join(fmt(s[k]) for k in ['row_count','channels','active_days','alarms','unknown_channel_rows'])+' |')
report=OUT/'ANALYSIS_AND_PLAN.md'
text=report.read_text(encoding='utf-8').replace('<!-- ANNUAL_TABLE -->','\n'.join(lines))
report.write_text(text,encoding='utf-8')
sys.path.insert(0,str(OUT/'deps'))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.spines.top':False,'axes.spines.right':False})
fig,ax=plt.subplots(2,2,figsize=(13.5,8.5),layout='constrained')
fig.suptitle('Москоллектор: что содержится в выданной истории',fontsize=19,fontweight='bold',x=.04,ha='left')
years=[x['year'] if x['year']!='2026' else '2026\n6 мес.' for x in profiles]
counts=[x['summary']['row_count']/1e6 for x in profiles]
unknown=[x['summary']['unknown_channel_rows']/1e6 for x in profiles]
a=ax[0,0]
a.bar(years,[n-u for n,u in zip(counts,unknown)],color='#3856a4',label='Канал есть в справочнике')
a.bar(years,unknown,bottom=[n-u for n,u in zip(counts,unknown)],color='#dda249',label='Нет связи со справочником')
a.set_title('313,5 млн строк, покрытие справочником меняется',loc='left',fontweight='bold')
a.set_ylabel('Млн строк'); a.legend(frameon=False,fontsize=8)
a=ax[0,1]
rates=[x['summary']['alarms']/x['summary']['row_count']*100 for x in profiles]
a.plot(years,rates,marker='o',color='#96532c',linewidth=2)
a.set_title('Частота флага тревоги нестабильна',loc='left',fontweight='bold')
a.set_ylabel('Тревожные строки, %'); a.set_ylim(0,2.65)
for i,v in enumerate(rates): a.annotate(f'{v:.2f}%',(i,v),xytext=(0,9),textcoords='offset points',ha='center',fontsize=9)
a=ax[1,0]
groups=['Газовые датчики','Датчики движения','Остальные + неизвестные']
total=sum(x['summary']['row_count'] for x in profiles)
shares=[224012118/total*100,51473736/total*100,(total-224012118-51473736)/total*100]
a.barh(groups,shares,color=['#3856a4','#688cbf','#aeb8c8']); a.invert_yaxis(); a.set_xlim(0,85)
a.set_title('87,86% строк — газ и движение',loc='left',fontweight='bold'); a.set_xlabel('Доля всех строк, %')
for i,v in enumerate(shares): a.text(v+1,i,f'{v:.2f}%',va='center')
a=ax[1,1]
import datetime
day_counts={x['event_date']:x['n'] for p in profiles if p['year']=='2024' for x in p['active_days'] if x['event_date']}
dates=[datetime.date(2024,4,1)+datetime.timedelta(days=i) for i in range(14)]
values=[day_counts.get(str(d),0)/1000 for d in dates]
a.bar([d.strftime('%d.%m') for d in dates],values,color=['#dda249' if d.day in [6,7,8,9] else '#3856a4' for d in dates])
a.set_title('Разрыв данных в апреле 2024',loc='left',fontweight='bold'); a.set_ylabel('Тысяч строк в сутки'); a.tick_params(axis='x',rotation=55)
for i,d in enumerate(dates):
    if d.day in [7,8]: a.text(i,3,'нет',ha='center',fontsize=8,color='#96532c')
fig.supxlabel('Все значения рассчитаны по исходным строкам до очистки. Флаг тревоги не подтверждает реальный инцидент.',fontsize=10)
fig.savefig(OUT/'data_overview.png',dpi=150,facecolor='white')
print('Report table and chart saved')
