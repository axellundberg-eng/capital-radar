import tempfile
import unittest
from pathlib import Path

from capital_radar.db import Database
from capital_radar.financing_corrections import (
    CorrectionError, approve_correction, apply_correction, create_correction,
    correction_detail, effective_overrides, revert_correction,
)
from capital_radar.transaction_feed import _status


class FinancingCorrectionsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.db=Database(Path(self.temp.name)/"db.sqlite"); self.db.initialize()
        with self.db.connect() as c:
            c.execute("INSERT INTO issuers(canonical_name,normalized_name,created_at,updated_at) VALUES('Test','test','n','n')")
            self.issuer=c.execute("SELECT id FROM issuers").fetchone()[0]
            c.execute("INSERT INTO documents(issuer_id,event_key,canonical_url,title,published_at,profile_category,cash_relevance_score,relevance_reason,created_at,updated_at) VALUES(?, 'doc','https://x','Test', '2026-01-01','financing',1,'t','n','n')",(self.issuer,)); self.doc=c.execute("SELECT id FROM documents").fetchone()[0]
            c.execute("INSERT INTO document_contents(document_id,source_url,status,created_at,updated_at) VALUES(?, 'https://x','extracted','n','n')",(self.doc,)); content=c.execute("SELECT id FROM document_contents").fetchone()[0]
            c.execute("INSERT INTO extraction_jobs(issuer_id,document_id,content_id,provider,model,prompt_version,input_sha256,selected_char_count,estimated_input_tokens,status,started_at) VALUES(?,?,?,'test','test','v1','hash',1,1,'completed','n')",(self.issuer,self.doc,content)); job=c.execute("SELECT id FROM extraction_jobs").fetchone()[0]
            c.execute("INSERT INTO financing_events(issuer_id,event_fingerprint,event_type,description,status,cash_direction,liquidity_effect_kind,reflected_in_report,confidence,gross_amount,created_at,updated_at) VALUES(?, 'event','share_issue','Test','announced','inflow','potential','unknown',1,200000000,'n','n')",(self.issuer,)); self.event=c.execute("SELECT id FROM financing_events").fetchone()[0]
            excerpt='Emissionen omfattar cirka 0,2 MSEK före kostnader.'
            c.execute("INSERT INTO financing_event_evidence(financing_event_id,document_id,extraction_job_id,evidence_label,source_excerpt,created_at) VALUES(?,?,?,'test',?,'n')",(self.event,self.doc,job,excerpt)); self.excerpt=excerpt
    def tearDown(self): self.temp.cleanup()
    def payload(self): return {"issuer_id":self.issuer,"financing_event_id":self.event,"correction_type":"replace_field","field_name":"gross_amount","proposed_value":200000,"expected_previous_value":200000000,"source_document_id":self.doc,"source_excerpt":self.excerpt,"confidence":.99,"rationale":"MSEK har tidigare skalats två gånger."}
    def test_approval_apply_and_revert_keep_extraction_immutable(self):
        with self.db.connect() as c:
            proposal=create_correction(c,self.payload(),actor='agent'); self.assertEqual(proposal['status'],'proposed')
            with self.assertRaises(CorrectionError): apply_correction(c,proposal['id'],actor='agent')
            approve_correction(c,proposal['id'],actor='user'); apply_correction(c,proposal['id'],actor='user')
            self.assertEqual(effective_overrides(c,[self.event])[self.event]['gross_amount'],200000.0)
            self.assertEqual(c.execute('SELECT gross_amount FROM financing_events WHERE id=?',(self.event,)).fetchone()[0],200000000)
            readback=correction_detail(c,proposal['id'])
            self.assertEqual(readback['original_values']['gross_amount'],200000000)
            self.assertEqual(readback['effective_values']['gross_amount'],200000.0)
            revert_correction(c,proposal['id'],actor='user'); self.assertEqual(effective_overrides(c,[self.event]),{})
    def test_replace_requires_exact_expected_value_and_evidence_fallback(self):
        with self.db.connect() as c:
            with self.assertRaises(CorrectionError): create_correction(c,self.payload()|{'expected_previous_value':1},actor='agent')
    def test_development_cancel_does_not_cancel_rights_issue(self):
        self.assertNotEqual(_status('Alligator avbryter självständig utveckling av mitazalimab och genomför en företrädesemission'), 'cancelled')


if __name__=='__main__': unittest.main()
