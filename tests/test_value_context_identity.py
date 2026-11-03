"""Reassert a verified fact after legacy A→B→A without recycling an old receipt."""
import hashlib
import json
import os
import unittest
from unittest.mock import patch

import test_instrument_scoped_grouping as fixtures
TARGET=fixtures.TARGET
from capital_radar.transaction_value_corrections import value_proposal_identity, _json


class ValueContextIdentityTests(unittest.TestCase):
    setUp=fixtures.InstrumentScopedGroupingTests.setUp
    cards=fixtures.InstrumentScopedGroupingTests.cards
    value=fixtures.InstrumentScopedGroupingTests.value
    correction=fixtures.InstrumentScopedGroupingTests.correction
    apply=fixtures.InstrumentScopedGroupingTests.apply

    def legacy_aba(self):
        with self.db.connect() as c:
            self.apply(c,self.correction(c))
            first=self.value(c,TARGET,2,'amount',16_500_000)
            # Reproduce the already committed older-source overwrite, made
            # before the source chronology guard was deployed. Synthetic only.
            with patch('capital_radar.transaction_value_corrections.validate_source_chronology'):
                self.value(c,TARGET,1,'amount',4_500_000)
            original=[tuple(x) for x in c.execute('SELECT * FROM financing_transaction_value_corrections WHERE id=?',(first,))]
        return first,original

    def test_same_field_context_is_stable_across_unrelated_changes(self):
        self.legacy_aba()
        args=(TARGET,1,'amount',4_500_000.0,16_500_000.0,2,self.texts[2])
        with self.db.connect() as c:
            before=value_proposal_identity(c,*args)
            self.value(c,'transaction:1:convertible:1',1,'amount',20_000_001)
            self.assertEqual(value_proposal_identity(c,*args),before)
            self.assertNotEqual(before,list(args))


if __name__=='__main__':unittest.main()
