"""Synthetic HTTP coverage for manufacturer freight boundaries."""
import json
import unittest
from ingestion import test_host_http as http_test

class PriceBoundaryTests(unittest.TestCase):
    setUp = http_test.IngestionHostHTTPTests.setUp
    tearDown = http_test.IngestionHostHTTPTests.tearDown
    _start_server = http_test.IngestionHostHTTPTests._start_server

    def test_reconciliation_is_gated_idempotent_and_snapshot_safe(self):
        owner=self.clients['owner']; employee=self.clients['employee']; dispatcher=self.clients['dispatcher']
        status,snapshot,_,_=owner.request('GET','/api/state')
        self.assertEqual(status,200)
        key='julong_demo_waybills_v2'
        rows=json.loads(snapshot['state'][key]); rows.append({'number':'SYN-PRICE-1','date':'2026-11-12','receiver':'Synthetic','destination':'Test','cargo':'Test','qty':'1','weight':1})
        snapshot['state'][key]=json.dumps(rows)
        status,created,_,_=owner.request('PUT','/api/state',{'base_rev':snapshot['rev'],'state':snapshot['state']})
        self.assertEqual(status,200)
        status,recon,_,_=owner.request('GET','/api/reconciliation/waybills');self.assertEqual(status,200)
        for client in (employee,dispatcher): self.assertEqual(client.request('GET','/api/reconciliation/waybills')[0],403)
        request={'number':'SYN-PRICE-1','amount':'872.31','base_rev':recon['rev']}; headers={'Idempotency-Key':'synthetic-price-1'}
        status,result,_,_=owner.request('POST','/api/reconciliation/waybills',request,headers);self.assertEqual(status,200)
        self.assertEqual(owner.request('POST','/api/reconciliation/waybills',request,headers)[1],result)
        self.assertEqual(owner.request('POST','/api/reconciliation/waybills',request,{'Idempotency-Key':'other'})[0],409)
        self.assertEqual(employee.request('POST','/api/reconciliation/waybills',request,{'Idempotency-Key':'employee'})[0],403)
        _,snapshot,_,_=owner.request('GET','/api/state'); self.assertNotIn('872.31',snapshot['state'][key])
        self.assertEqual(owner.request('PUT','/api/state',{'base_rev':snapshot['rev'],'state':snapshot['state']})[0],200)
        _,recon,_,_=owner.request('GET','/api/reconciliation/waybills')
        self.assertEqual(next(x for x in recon['items'] if x['number']=='SYN-PRICE-1')['amount'],'872.31')
        self.assertNotIn(b'872.31',owner.request('GET','/manual-v9')[3])

if __name__=='__main__': unittest.main()
