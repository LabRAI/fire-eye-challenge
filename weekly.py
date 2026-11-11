"""Hosted, bounded evaluation of forecast data. No participant code or model runs."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import time
import numpy as np
import requests
from pyproj import Transformer
from huggingface_hub import HfApi, CommitOperationAdd
from contract import GRID, pairs, top_capture

UTC = dt.timezone.utc
REPO = 'yx21e/fire-eye-challenge'
FORECAST = 'https://huggingface.co/datasets/yx21e/fire-eye-forecasts-preview/resolve/main/latest.json'
INCIDENTS = 'https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Incident_Locations_YearToDate/FeatureServer/0/query'
METHOD = 'weekly-wfigs-positive-capture-v1'

def stamp(value=None):
    return (value or dt.datetime.now(UTC)).astimezone(UTC).isoformat().replace('+00:00', 'Z')

def instant(value):
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('UTC timestamp required')
    return parsed.astimezone(UTC)

def encode(value):
    return json.dumps(value, separators=(',', ':'), allow_nan=False).encode()

def sha(value):
    return hashlib.sha256(value).hexdigest()

def download(url, limit=4000000):
    with requests.get(url, timeout=(10, 35), stream=True) as response:
        response.raise_for_status(); content = bytearray()
        for chunk in response.iter_content(65536):
            content.extend(chunk)
            if len(content) > limit:
                raise ValueError('Source size limit exceeded')
    return bytes(content)

def parse(raw):
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON')))

def round_definition(start, mask, now):
    year, week, _ = start.isocalendar()
    mask_hash = sha(mask)
    return {'schema':'fire-eye-weekly-round-v1', 'id':f'{year}-W{week:02d}',
            'starts_at_utc':stamp(start), 'ends_at_utc':stamp(start+dt.timedelta(days=7)),
            'submission_deadline_utc':stamp(start-dt.timedelta(minutes=20)),
            'created_at_utc':stamp(now), 'grid':GRID, 'mask_path':f'masks/{mask_hash}.bin',
            'mask_sha256':mask_hash, 'max_cells':4096, 'max_bytes':524288,
            'windows':[{'lead_hours':12*(i+1), 'start_utc':stamp(start+dt.timedelta(hours=12*i)),
                        'end_utc':stamp(start+dt.timedelta(hours=12*(i+1)))} for i in range(14)],
            'method':METHOD, 'track':'open-input prospective incident comparison',
            'model_execution':False, 'public_predictions':True}

def validate_receipt(record, round_, mask, committed_at):
    data = record['submission']
    if record.get('schema') != 'fire-eye-weekly-receipt-v1' or sha(encode(data)) != record['submission_sha256']:
        raise ValueError('Receipt checksum mismatch')
    if committed_at >= instant(round_['submission_deadline_utc']) or instant(record['received_at_utc']) > committed_at+dt.timedelta(seconds=1):
        raise ValueError('Receipt was not published before the cutoff')
    if data.get('schema') != 'fire-eye-weekly-v1' or data['round_id'] != round_['id']:
        raise ValueError('Round mismatch')
    r = data['region']
    if set(r) != {'row','column','width','height'} or any(type(x) is not int for x in r.values()):
        raise ValueError('Invalid region')
    y,x,h,w = r['row'],r['column'],r['height'],r['width']
    if min(y,x)<0 or min(h,w)<1 or h*w>4096 or y+h>GRID['height'] or x+w>GRID['width']:
        raise ValueError('Region exceeds supported grid')
    valid = np.frombuffer(mask,dtype='u1').reshape(GRID['height'],GRID['width'])[y:y+h,x:x+w].reshape(-1)==1
    if not valid.any() or len(data['scores'])!=14:
        raise ValueError('Incomplete weekly forecast')
    for row in data['scores']:
        if len(row)!=h*w:
            raise ValueError('Window shape mismatch')
        for i,v in enumerate(row):
            if valid[i]:
                if type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1 or abs(v*1000-round(v*1000))>1e-7:
                    raise ValueError('Invalid land forecast')
            elif v is not None:
                raise ValueError('Masked cells must remain null')
    return valid

def query(params):
    for attempt in range(3):
        try:
            r=requests.get(INCIDENTS,params={'f':'json',**params},timeout=(10,35),allow_redirects=False)
            r.raise_for_status(); value=r.json()
            if not isinstance(value,dict) or value.get('error'):
                raise ValueError('Observation query failed')
            return value
        except (requests.RequestException,ValueError):
            if attempt==2: raise RuntimeError('Observations unavailable') from None
            time.sleep(2**attempt)

def observations(start,end,now):
    if start.year != now.year:
        raise RuntimeError('A previous-year observation archive is required')
    where="IncidentTypeCategory='WF' AND FireDiscoveryDateTime >= TIMESTAMP '"+start.strftime('%Y-%m-%d %H:%M:%S')+"' AND FireDiscoveryDateTime < TIMESTAMP '"+end.strftime('%Y-%m-%d %H:%M:%S')+"'"
    ids=query({'where':where,'returnIdsOnly':'true','geometry':'-125.5,24,-66.5,49.8','geometryType':'esriGeometryEnvelope','inSR':'4326','spatialRel':'esriSpatialRelIntersects'}).get('objectIds')
    if not isinstance(ids,list) or any(type(i) is not int for i in ids) or len(set(ids))!=len(ids) or len(ids)>20000:
        raise RuntimeError('Observation inventory invalid')
    rows=[]
    for offset in range(0,len(ids),100):
        batch=ids[offset:offset+100]
        page=query({'objectIds':','.join(map(str,batch)),'outFields':'OBJECTID,IrwinID,IncidentTypeCategory,FireDiscoveryDateTime','returnGeometry':'true','outSR':'4326'})
        features=page.get('features',[])
        if page.get('exceededTransferLimit') or len(features)!=len(batch) or {f.get('attributes',{}).get('OBJECTID') for f in features}!=set(batch):
            raise RuntimeError('Incomplete observation inventory')
        rows.extend(features)
    return {'source':INCIDENTS,'retrieved_at_utc':stamp(now),'features':rows}

def positive_cells(inventory,region,start,end):
    transform=Transformer.from_crs('EPSG:4326','EPSG:5070',always_xy=True)
    cells=set();seen=set()
    for item in inventory['features']:
        a,g=item['attributes'],item['geometry']
        discovered=dt.datetime.fromtimestamp(a['FireDiscoveryDateTime']/1000,UTC)
        if a['IncidentTypeCategory']!='WF' or not start<=discovered<end:continue
        if not isinstance(a.get('IrwinID'),str) or not -125.5<=g['x']<=-66.5 or not 24<=g['y']<=49.8:raise ValueError('Invalid observation point')
        if a['IrwinID'] in seen:continue
        seen.add(a['IrwinID']);x,y=transform.transform(g['x'],g['y'])
        c=math.floor((x-GRID['transform'][2])/5000)-region['column'];r=math.floor((GRID['transform'][5]-y)/5000)-region['row']
        if 0<=r<region['height'] and 0<=c<region['width']:cells.add(r*region['width']+c)
    return cells

def metrics(values,valid,cells):
    indices=np.flatnonzero(valid).tolist();mapping={v:i for i,v in enumerate(indices)}
    positives={mapping[v] for v in cells if v in mapping};scores=[values[i] for i in indices]
    return {'eligible_land_cells':len(indices),'observed_fire_cells':len(positives),
            'capture_top_10pct':top_capture(scores,positives,.1), 'capture_top_20pct':top_capture(scores,positives,.2)}

def run():
    if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('RUNNER_ENVIRONMENT')!='github-hosted':
        raise RuntimeError('Real collection/evaluation is restricted to GitHub-hosted runners')
    api=HfApi(token=os.environ['HF_TOKEN']);now=dt.datetime.now(UTC);head=api.repo_info(REPO,repo_type='dataset').sha
    entries={e.path:e for e in api.list_repo_tree(REPO,repo_type='dataset',revision=head,recursive=True,expand=True) if hasattr(e,'size')}
    base='https://huggingface.co/datasets/'+REPO+'/resolve/'+head+'/'
    cache={};pending={};results={}
    def read(path):
        if path in pending:return pending[path]
        if path not in cache:cache[path]=download(base+path)
        return cache[path]
    def put(path,value):pending[path]=value if isinstance(value,bytes) else encode(value)
    previous=parse(read('index.json')) if 'index.json' in entries else {}
    rounds={path.split('/')[1]:parse(read(path)) for path in entries if path.startswith('rounds/') and path.endswith('/round.json')}
    # Round generation and reference capture do not run a forecasting model.
    try:
        manifest=parse(download(FORECAST));grid=manifest['runs'][0]['overview']['grid']
        if any(grid.get(k)!=v for k,v in GRID.items()) or grid.get('nodata')!=65535 or grid.get('scale')!=.001:raise ValueError('Native grid changed')
        def native(run):
            spec=run['overview']['values'];url=manifest['asset_base']+spec['path']
            if not url.startswith('https://huggingface.co/datasets/yx21e/fire-eye-forecasts-preview/resolve/'):raise ValueError('Untrusted reference')
            raw=download(url)
            if len(raw)!=GRID['height']*GRID['width']*2 or sha(raw)!=spec['sha256']:raise ValueError('Reference checksum mismatch')
            return raw
        next_start=(now+dt.timedelta(days=7-now.weekday())).replace(hour=0,minute=0,second=0,microsecond=0)
        raw=native(manifest['runs'][0]);mask=(np.frombuffer(raw,dtype='<u2')!=65535).astype('u1').tobytes()
        definition=round_definition(next_start,mask,now)
        if definition['id'] not in rounds:
            put(definition['mask_path'],mask);put('rounds/'+definition['id']+'/round.json',definition);rounds[definition['id']]=definition
        for definition in rounds.values():
            deadline=instant(definition['submission_deadline_utc'])
            if now>=deadline or instant(manifest['published_at_utc'])>now:continue
            for run_ in manifest['runs']:
                target=stamp(instant(run_['target_time_utc']))
                if target not in {w['end_utc'] for w in definition['windows']}:continue
                raw=native(run_);digest=sha(raw);path='references/'+definition['id']+'/'+target.replace(':','')+'-'+digest+'.json'
                if path in entries:continue
                put('reference-values/'+digest+'.bin',raw)
                put(path,{'issue_time_utc':manifest['issue_time_utc'],'target_time_utc':target,'published_at_utc':manifest['published_at_utc'],'captured_at_utc':stamp(now),'path':'reference-values/'+digest+'.bin','sha256':digest})
        reference_status='current'
    except Exception:
        reference_status='previous publication retained'
    if not rounds:raise RuntimeError('No valid weekly round could be prepared; publication stopped')
    observation_cache={};failures=0
    receipt_paths=sorted(p for p in entries if p.startswith('submissions/') and p.endswith('.json'))
    if len(receipt_paths)>2000:raise RuntimeError('Submission inventory exceeds this runner budget')
    for path in receipt_paths:
        result_path='results/'+path.split('/')[-1];previous_result=parse(read(result_path)) if result_path in entries else None
        if previous_result and previous_result.get('status')=='finalized':results[result_path]=previous_result;continue
        try:
            if entries[path].size>524288:raise ValueError('Receipt exceeds size budget')
            receipt=parse(read(path));definition=rounds[receipt['submission']['round_id']];mask=read(definition['mask_path'])
            if sha(mask)!=definition['mask_sha256']:raise ValueError('Round mask integrity error')
            commit_date=entries[path].last_commit.date
            if isinstance(commit_date,str):commit_date=instant(commit_date)
            valid=validate_receipt(receipt,definition,mask,commit_date)
            data=receipt['submission'];record={'id':receipt['id'],'model_name':data['model_name'],'model_version':data['model_version'],'round_id':definition['id'],'region':data['region'],'received_at_utc':receipt['received_at_utc'],'published_at_utc':stamp(commit_date),'receipt_url':base+path,'method':METHOD,'windows':[]}
            for i,window in enumerate(definition['windows']):
                old=next((x for x in (previous_result or {}).get('windows',[]) if x['end_utc']==window['end_utc']),None)
                if old and old.get('status')=='finalized':record['windows'].append(old);continue
                start,end=instant(window['start_utc']),instant(window['end_utc']);item={**window,'status':'awaiting observations','participant':None,'firewxfm':None}
                if now>=end+dt.timedelta(hours=72):
                    try:
                        key=window['start_utc']
                        if key not in observation_cache:observation_cache[key]=observations(start,end,now)
                        inventory=observation_cache[key];observation_raw=encode(inventory);digest=sha(observation_raw)
                        put('observations/'+digest+'.json',observation_raw)
                        cells=positive_cells(inventory,data['region'],start,end)
                        item.update(participant=metrics(data['scores'][i],valid,cells),observations_path='observations/'+digest+'.json',observations_sha256=digest,evaluated_at_utc=stamp(now),status='finalized' if now>=end+dt.timedelta(days=7) else 'preliminary')
                        references=[]
                        for rp in entries:
                            if not rp.startswith('references/'+definition['id']+'/') or not rp.endswith('.json'):continue
                            ref=parse(read(rp))
                            if ref['target_time_utc']==window['end_utc'] and max(instant(ref['captured_at_utc']),instant(ref['published_at_utc']))<instant(definition['submission_deadline_utc']):references.append(ref)
                        if references:
                            ref=max(references,key=lambda r:r['issue_time_utc']);raw=read(ref['path'])
                            if sha(raw)!=ref['sha256']:raise ValueError('Reference integrity mismatch')
                            region=data['region'];arr=np.frombuffer(raw,dtype='<u2').reshape(GRID['height'],GRID['width'])[region['row']:region['row']+region['height'],region['column']:region['column']+region['width']].reshape(-1)
                            # Preserve equal support: do not compare when a baseline has missing land cells.
                            if np.all(arr[valid]!=65535):item.update(firewxfm=metrics((arr*.001).tolist(),valid,cells),reference=ref)
                    except Exception:
                        failures+=1;item=old or item
                record['windows'].append(item)
            record['status']='finalized' if all(w['status']=='finalized' for w in record['windows']) else 'preliminary' if any(w['participant'] for w in record['windows']) else 'awaiting observations'
        except Exception:
            failures+=1
            if previous_result:record=previous_result
            else:record={'id':path.split('/')[-1][:-5],'status':'review required','method':METHOD,'windows':[]}
        if record!=previous_result:put(result_path,record)
        results[result_path]=record
    all_paths={**entries,**pending}
    index={'schema':'fire-eye-weekly-index-v1','updated_at_utc':stamp(now),'dataset':REPO,'method':METHOD,'model_execution':False,'reference_status':reference_status,'deferred_checks':failures,
           'rounds':sorted(rounds.values(),key=lambda r:r['starts_at_utc'],reverse=True),'results':list(results.values())}
    put('index.json',index)
    put('README.md',b'---\nlicense: cc-by-4.0\ntask_categories:\n- time-series-forecasting\n---\n# Fire-eye weekly forecast challenge\n\nPublic forecast data, immutable receipts, rounds, observed-fire evidence and comparisons.\nParticipants run their own models. No model code or weights are executed here.\nRegional 5 km submissions contain fourteen 12-hour windows covering seven days.\nComparisons use documented wildfire-discovery cells and show FireWx-FM only where an advance reference exists.\n')
    operations=[CommitOperationAdd(path_in_repo=p,path_or_fileobj=b) for p,b in pending.items() if p not in entries or sha(read(p) if p not in pending else cache.get(p,b''))!=sha(b)]
    # The pinned parent detects concurrent participant uploads. A conflict retries next schedule.
    result=api.create_commit(REPO,repo_type='dataset',parent_commit=head,commit_message='Refresh weekly rounds and observed-fire comparisons',operations=operations)
    print(json.dumps({'revision':result.oid,'rounds':len(rounds),'receipts':len(receipt_paths),'deferred_checks':failures,'runner':'github-hosted'}))

if __name__=='__main__':
    run()
