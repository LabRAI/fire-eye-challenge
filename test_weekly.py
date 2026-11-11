import datetime as dt
import unittest
from unittest.mock import patch
import numpy as np
import weekly as w

class WeeklyContractTests(unittest.TestCase):
    def setUp(self):
        self.now=dt.datetime(2026,11,10,tzinfo=w.UTC)
        self.start=dt.datetime(2026,11,16,tzinfo=w.UTC)
        self.mask=bytearray(w.GRID['width']*w.GRID['height']);self.mask[:2]=bytes([1,1])
        self.round=w.round_definition(self.start,bytes(self.mask),self.now)
        data={'schema':'fire-eye-weekly-v1','round_id':self.round['id'],'model_name':'ConvLSTM','model_version':'v1','region':{'row':0,'column':0,'width':3,'height':1},'scores':[[0.2,0.8,None] for _ in range(14)]}
        self.receipt={'schema':'fire-eye-weekly-receipt-v1','submission':data,'submission_sha256':w.sha(w.encode(data)),'received_at_utc':w.stamp(self.now)}

    def update_hash(self):self.receipt['submission_sha256']=w.sha(w.encode(self.receipt['submission']))

    def test_round_contains_exact_seven_days(self):
        self.assertEqual(len(self.round['windows']),14)
        self.assertEqual(w.instant(self.round['windows'][0]['start_utc']),self.start)
        self.assertEqual(w.instant(self.round['windows'][-1]['end_utc']),self.start+dt.timedelta(days=7))
        self.assertEqual(w.instant(self.round['submission_deadline_utc']),self.start-dt.timedelta(minutes=20))

    def test_land_missing_is_not_zero(self):
        valid=w.validate_receipt(self.receipt,self.round,bytes(self.mask),self.now)
        self.assertEqual(valid.tolist(),[True,True,False])
        self.receipt['submission']['scores'][0][0]=None;self.update_hash()
        with self.assertRaises(ValueError):w.validate_receipt(self.receipt,self.round,bytes(self.mask),self.now)

    def test_cutoff_and_hash_are_enforced(self):
        with self.assertRaises(ValueError):w.validate_receipt(self.receipt,self.round,bytes(self.mask),self.start)
        self.receipt['submission']['scores'][0][0]=0.5
        with self.assertRaises(ValueError):w.validate_receipt(self.receipt,self.round,bytes(self.mask),self.now)

    def test_no_fire_is_no_score_and_ties_receive_area_baseline(self):
        valid=np.ones(10,dtype=bool)
        empty=w.metrics([0.5]*10,valid,set());self.assertIsNone(empty['capture_top_20pct'])
        tied=w.metrics([0.5]*10,valid,{1,5});self.assertAlmostEqual(tied['capture_top_20pct'],0.2)
        high=w.metrics([1,0,0,0,0,0,0,0,0,0],valid,{0});self.assertEqual(high['capture_top_20pct'],1)

    def test_no_real_worker_on_lab_or_self_hosted(self):
        for env in [{},{'GITHUB_ACTIONS':'true','RUNNER_ENVIRONMENT':'self-hosted'}]:
            with patch.dict(w.os.environ,env,clear=True):
                with self.assertRaises(RuntimeError):w.run()

    def test_previous_year_requires_archive_not_empty_observations(self):
        with self.assertRaises(RuntimeError):w.observations(dt.datetime(2025,12,31,tzinfo=w.UTC),self.start,self.now)

if __name__=='__main__':unittest.main()
