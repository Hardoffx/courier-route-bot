from app.db import normalize_phone, route_profile
from app.ocr import _facility_code


def test_cmd_facility_code_uses_first_part_before_slash():
    assert _facility_code("5827/5958/6113") == "5827"
    assert _facility_code("3433/4338") == "3433"
    assert _facility_code("458") == "458"


def test_phone_normalization_for_android_dialing():
    assert normalize_phone("8 495 123-45-67") == "+74951234567"
    assert normalize_phone("+7 (495) 123-45-67") == "+74951234567"
    assert normalize_phone("4951234567") == "+74951234567"
    assert normalize_phone("123") is None


def test_route_profiles_are_separate_for_weekdays_and_weekends():
    assert route_profile("2026-09-11") == "weekday"
    assert route_profile("2026-09-12") == "weekend"
    assert route_profile("2026-09-13") == "weekend"
