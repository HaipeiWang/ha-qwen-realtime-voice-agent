import unittest
from datetime import datetime, timezone

import httpx

from app.tools.facts import FactTools
from app.tools.ha_facts import HAFactsBackend
from app.tools.registry import ToolRegistry, normalize_result
from app.tools.schema import CanonicalToolRequest
from app.tools.schema import CanonicalTool
from app.tools.legacy import callback_backend, outcome_payload
import asyncio


class ToolLayerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.exposed = {"weather.home"}
        self.forecast = [{"datetime": "2026-09-09T00:00:00Z", "temperature": 23}]

        def transport(request):
            self.calls.append(request)
            path = request.url.path
            if path == "/api/config":
                return httpx.Response(200, json={"time_zone": "Asia/Shanghai"})
            if path.startswith("/api/states/"):
                return httpx.Response(200, json={"entity_id": path.rsplit("/", 1)[-1], "state": "sunny",
                    "attributes": {"friendly_name": "家", "temperature": 24, "temperature_unit": "°C", "supported_features": 3}})
            return httpx.Response(200, json={"service_response": {"weather.home": {"forecast": self.forecast}}})

        self.client = httpx.AsyncClient(base_url="http://ha/", transport=httpx.MockTransport(transport))
        async def exposed():
            return self.exposed
        self.backend = HAFactsBackend(self.client, exposed)
        self.registry = ToolRegistry()
        FactTools(self.backend, lambda: datetime(2026, 7, 1, 12, tzinfo=timezone.utc)).register(self.registry)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def execute(self, name, **arguments):
        return await self.registry.execute(CanonicalToolRequest("c", "t", "call", name, arguments))

    async def test_local_clock_uses_ha_timezone(self):
        result = await self.execute("GetCurrentTime")
        self.assertEqual(result.data["datetime"], "2026-07-01T20:00:00+08:00")
        self.assertTrue(result.result.verified)

    async def test_overseas_clock_observes_dst(self):
        result = await self.execute("GetCurrentTime", timezone="America/New_York")
        self.assertEqual(result.data["datetime"], "2026-07-01T08:00:00-04:00")
        self.assertEqual(self.calls, [])

    async def test_invalid_zone_does_not_guess(self):
        result = await self.execute("GetCurrentTime", timezone="not/a/timezone")
        self.assertEqual(result.result.status, "failed")

    async def test_current_weather_has_units(self):
        result = await self.execute("GetWeather")
        self.assertEqual(result.data["condition"], "sunny")
        self.assertEqual(result.data["measurements"]["temperature_unit"], "°C")
        self.assertNotIn("forecast", result.data)

    async def test_forecast_is_service_response_not_current_state(self):
        result = await self.execute("GetWeather", type="daily")
        self.assertEqual(result.data["forecast"], self.forecast)
        self.assertTrue(any(r.method == "POST" and "return_response" in r.url.params for r in self.calls))

    async def test_missing_forecast_is_unknown(self):
        self.forecast = []
        result = await self.execute("GetWeather", type="daily")
        self.assertEqual(result.result.status, "unknown")

    async def test_unexposed_weather_not_fetched(self):
        self.exposed = set()
        result = await self.execute("GetWeather", entity_id="weather.private")
        self.assertEqual(result.result.status, "failed")
        self.assertEqual(self.calls, [])

    async def test_multiple_locations_require_choice(self):
        self.exposed.add("weather.office")
        result = await self.execute("GetWeather")
        self.assertEqual(result.result.detail, "weather_target_required")

    async def test_weather_catalog_has_real_ids_and_capabilities(self):
        result = await self.execute("GetWeather", type="catalog")
        self.assertEqual(result.data["choices"][0]["entity_id"], "weather.home")
        self.assertEqual(result.data["choices"][0]["types"], ["current", "daily", "hourly"])
        self.assertFalse(any(r.method == "POST" for r in self.calls))

    async def test_home_and_here_select_unique_weather(self):
        for location in ("家里", "这里"):
            result = await self.execute("GetWeather", location=location)
            self.assertEqual(result.result.status, "completed")
            self.assertEqual(result.data["entity_id"], "weather.home")

    async def test_city_and_ambiguous_region_do_not_fall_back_to_home(self):
        for location in ("上海", "徐汇区", "附近", "纽约"):
            result = await self.execute("GetWeather", location=location, entity_id="weather.home", type="daily")
            self.assertEqual(result.result.detail, "weather_location_unverified")
            self.assertNotIn("forecast", result.data)
        self.assertFalse(any(r.method == "POST" for r in self.calls))

    async def test_wrong_id_returns_usable_choices(self):
        result = await self.execute("GetWeather", entity_id="weather.invented")
        self.assertEqual(result.result.detail, "weather_not_exposed")
        self.assertEqual(result.data["choices"][0]["entity_id"], "weather.home")
        self.assertFalse(any("invented" in str(r.url) for r in self.calls))

    async def test_unsupported_forecast_never_calls_service(self):
        result = await self.execute("GetWeather", type="twice_daily")
        self.assertEqual(result.result.detail, "weather_forecast_unsupported")
        self.assertFalse(any(r.method == "POST" for r in self.calls))

    async def test_current_cannot_answer_tomorrow(self):
        result = await self.execute("GetWeather", type="current", day="tomorrow")
        self.assertEqual(result.result.detail, "weather_forecast_required")
        self.assertNotIn("measurements", result.data)

    async def test_tomorrow_filters_in_home_timezone(self):
        self.forecast = [{"datetime": "2026-07-01T15:00:00Z", "temperature": 11},
                         {"datetime": "2026-07-01T16:00:00Z", "temperature": 22}]
        result = await self.execute("GetWeather", type="daily", day="tomorrow")
        self.assertEqual(result.data["requested_date"], "2026-07-02")
        self.assertEqual(result.data["forecast"], [self.forecast[1]])
        self.assertNotIn("measurements", result.data)

    async def test_missing_requested_date_never_uses_other_date(self):
        result = await self.execute("GetWeather", type="daily", day="tomorrow")
        self.assertEqual(result.result.detail, "forecast_date_missing")

    async def test_weather_transport_failure_is_not_no_integration(self):
        async def fail(entity, kind):
            raise httpx.ReadTimeout("private transport details")
        self.backend.weather_forecast = fail
        result = await self.execute("GetWeather", type="daily")
        self.assertEqual(result.result.status, "unknown")
        self.assertEqual(result.result.detail, "weather_fetch_failed")
        self.assertNotIn("private", str(result.data))

    async def test_unknown_tool_and_invalid_parameters_never_dispatch(self):
        self.assertEqual((await self.execute("ScheduleDevice")).result.status, "failed")
        self.assertEqual((await self.execute("GetWeather", type="invented")).result.status, "failed")
        self.assertEqual(self.calls, [])

    async def test_legacy_handler_preserves_wire_name_and_returns_partial(self):
        async def handler(params):
            self.assertEqual(params.function_name, "intent__HassTurnOff")
            await params.result_callback({"response_type": "action_done", "data": {
                "success": [{"id": "light.a"}], "failed": [{"id": "light.b"}]}})
            await params.result_callback({"success": True, "result": "duplicate"})
        self.registry.register(CanonicalTool("intent__HassTurnOff", "", {"type": "object"}), callback_backend(handler))
        outcome = await self.execute("intent__HassTurnOff")
        self.assertEqual(outcome_payload(outcome)["status"], "partial")
        self.assertFalse(outcome_payload(outcome)["success"])

    async def test_timeout_is_unknown_without_retry(self):
        calls = []
        async def handler(request, execution_id):
            calls.append(request)
            await asyncio.Event().wait()
        self.registry.timeout_s = 0.01
        self.registry.register(CanonicalTool("Slow", "", {"type": "object"}), handler)
        outcome = await self.execute("Slow")
        self.assertEqual(outcome.result.status, "unknown")
        self.assertEqual(len(calls), 1)

    async def test_revoked_exposure_is_rechecked(self):
        self.exposed.clear()
        with self.assertRaises(PermissionError):
            await self.backend.weather_forecast("weather.home", "daily")
        self.assertEqual(self.calls, [])

    async def test_duplicate_registration_fails(self):
        with self.assertRaises(ValueError):
            FactTools(self.backend).register(self.registry)


class ResultTests(unittest.TestCase):
    def test_error_text_is_not_success(self):
        self.assertEqual(normalize_result("Error calling tool: Service handler cannot target all devices", "e").result.status, "failed")

    def test_empty_and_boolean_success_are_unknown(self):
        for value in ({}, None, "", {"success": True}, {"status": "completed"}):
            self.assertEqual(normalize_result(value, "e").result.status, "unknown")

    def test_mcp_error_overrides_content(self):
        result = normalize_result({"isError": True, "content": [{"type": "text", "text": '{"success": true}'}]}, "e")
        self.assertEqual(result.result.status, "failed")

    def test_partial_and_accepted_cannot_authorize_full_success(self):
        partial = normalize_result({"response_type": "action_done", "data": {
            "success": [{"id": "light.a"}], "failed": [{"id": "light.b"}]}}, "e")
        self.assertEqual(partial.result.status, "partial")
        self.assertFalse(partial.result.permits_success_confirmation)
        self.assertFalse(normalize_result({"status": "accepted"}, "e").result.permits_success_confirmation)

    def test_action_done_is_not_physical_verification(self):
        result = normalize_result({"response_type": "action_done", "data": {"success": [{"id": "light.a"}]}}, "e")
        self.assertEqual(result.result.status, "completed")
        self.assertFalse(result.result.verified)


if __name__ == "__main__":
    unittest.main()
