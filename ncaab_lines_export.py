"""NCAAB lines export (Nov 28): from the 5-minute Supabase snapshots -> one row per game with the OPENER (book 30) and the
TRUE CLOSE (last consensus book-15 line captured before tip), spread + moneyline. Pushed to the engine repo as
home/ncaab/an_lines_SEASON.csv, so the engine grades line value vs the real close and can shadow-test past days.
env: SUPABASE_URL, SUPABASE_KEY, ENGINE_PAT"""
import os, io, csv, json, base64, requests, datetime as dt
from zoneinfo import ZoneInfo
SB_URL=(os.environ.get('SUPABASE_URL') or 'https://qkvkupbekkozhmqqvxqh.supabase.co').rstrip('/'); KEY=os.environ['SUPABASE_KEY']
SB={'apikey':KEY,'Authorization':f'Bearer {KEY}'}
SINCE=os.environ.get('SINCE','2026-11-01'); SEASON=os.environ.get('SEASON','2026-27')
def get(table,params):
    out=[]; off=0
    while True:
        r=requests.get(f'{SB_URL}/rest/v1/{table}',params=params,headers={**SB,'Range':f'{off}-{off+999}'},timeout=120); r.raise_for_status()
        rows=r.json(); out+=rows
        if len(rows)<1000: return out
        off+=1000
G=get('live_games',{'select':'game_id,start_time,home_team,away_team,status,home_score,away_score','sport':'eq.ncaab','start_time':f'gte.{SINCE}','order':'game_id.asc'})
print('games',len(G))
O=get('live_odds',{'select':'game_id,book_id,market,side,value,odds,captured_at','sport':'eq.ncaab','book_id':'in.(15,30)','market':'in.(spread,moneyline)',
                   'is_live':'eq.false','captured_at':f'gte.{SINCE}','order':'captured_at.asc'})
print('odds rows',len(O))
by={}
for o in O: by.setdefault(o['game_id'],[]).append(o)
ET=ZoneInfo('America/New_York'); rows=[]
for g in G:
    st=g['start_time']; os_=by.get(g['game_id'],[])
    if not st or not os_: continue
    stt=dt.datetime.fromisoformat(st.replace('Z','+00:00'))
    pre=[o for o in os_ if dt.datetime.fromisoformat(o['captured_at'].replace('Z','+00:00'))<stt]
    def first(book,mk,side,f):
        x=[o[f] for o in pre if o['book_id']==book and o['market']==mk and o['side']==side and o[f] is not None]; return x[0] if x else None
    def last(book,mk,side,f):
        x=[o[f] for o in pre if o['book_id']==book and o['market']==mk and o['side']==side and o[f] is not None]; return x[-1] if x else None
    r={'game_id':g['game_id'],'date_et':stt.astimezone(ET).strftime('%Y-%m-%d'),'start_utc':st,'home':g['home_team'],'away':g['away_team'],
       'open_spread_home':first(30,'spread','home','value') if first(30,'spread','home','value') is not None else first(15,'spread','home','value'),
       'close_spread_home':last(15,'spread','home','value'),
       'open_ml_home':first(30,'moneyline','home','odds') if first(30,'moneyline','home','odds') is not None else first(15,'moneyline','home','odds'),
       'open_ml_away':first(30,'moneyline','away','odds') if first(30,'moneyline','away','odds') is not None else first(15,'moneyline','away','odds'),
       'close_ml_home':last(15,'moneyline','home','odds'),'close_ml_away':last(15,'moneyline','away','odds'),
       'first_seen_utc':pre[0]['captured_at'] if pre else None,'home_score':g.get('home_score'),'away_score':g.get('away_score')}
    rows.append(r)
buf=io.StringIO(); w=csv.DictWriter(buf,fieldnames=list(rows[0].keys()) if rows else ['game_id']); w.writeheader(); w.writerows(rows)
print('rows',len(rows))
# push to the engine repo
REPO='thebettorsbureau/bettors-bureau-nba-engine'; PATH=f'home/ncaab/an_lines_{SEASON}.csv'
H={'Authorization':f"token {os.environ['ENGINE_PAT']}",'Accept':'application/vnd.github+json'}
cur=requests.get(f'https://api.github.com/repos/{REPO}/contents/{PATH}',headers=H,timeout=60)
body={'message':f'NCAAB lines export ({len(rows)} games)','content':base64.b64encode(buf.getvalue().encode()).decode()}
if cur.status_code==200: body['sha']=cur.json()['sha']
for k in range(3):
    p=requests.put(f'https://api.github.com/repos/{REPO}/contents/{PATH}',headers=H,data=json.dumps(body),timeout=120)
    if p.status_code<300: print('pushed',PATH); break
    cur=requests.get(f'https://api.github.com/repos/{REPO}/contents/{PATH}',headers=H,timeout=60)
    if cur.status_code==200: body['sha']=cur.json()['sha']
else: raise SystemExit(f'push failed {p.status_code} {p.text[:200]}')
