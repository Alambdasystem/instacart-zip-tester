import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock

from connector import ConnectorError, establish_location
from location_context import set_location
from telemetry import emit


class Empty:
    def count(self):return 0
    @property
    def first(self):return self
    def wait_for(self,**kwargs):raise TimeoutError()


class ConnectorTelemetryTests(unittest.TestCase):
    def test_address_failure_diagnostic_is_structured_and_redacted(self):
        page=Mock();page.locator.return_value.inner_text.return_value=''
        page.get_by_role.return_value=Empty()
        set_location({'postal_code':'63129','country_code':'US','address':{'line1':'5430 Telegraph Road','city':'St. Louis','region':'MO'}})
        try:
            with self.assertRaises(ConnectorError) as caught:establish_location(page,'63129',False)
        finally:set_location(None)
        self.assertEqual(caught.exception.status,'location_required')
        self.assertEqual(caught.exception.diagnostic['address_substage'],'opening_picker')
        self.assertFalse(caught.exception.diagnostic['save_clicked'])
        output=io.StringIO()
        with redirect_stdout(output):
            emit('search_failed',status=caught.exception.status,**caught.exception.diagnostic,address='5430 Telegraph Road',session='secret')
        row=json.loads(output.getvalue())
        self.assertEqual(row['address_substage'],'opening_picker')
        self.assertNotIn('5430 Telegraph Road',output.getvalue())
        self.assertNotIn('secret',output.getvalue())


if __name__=='__main__':unittest.main()

