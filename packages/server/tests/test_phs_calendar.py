import datetime as dt

import pytest

from agent_media_server.phs_calendar import parse_grid


def test_only_own_times_no_client_details():
    body = '''<option data-staff="1603">Owner</option>
<td class="cell booked staff-cell" data-reference="123" data-staff="1603"><span class="booking-time">2:45pm – 4:15pm</span><p>Private client medical details</p>
<td class="cell booked staff-cell" data-reference="124" data-staff="2"><span class="booking-time">3:00pm – 4:00pm</span>'''
    result = parse_grid(body, dt.date(2026, 10, 22), "1603")
    assert len(result) == 1
    assert result[0]["start"] == "2026-10-22T14:45:00+11:00"
    assert result[0]["end"] == "2026-10-22T16:15:00+11:00"
    assert "Private" not in str(result) and "medical" not in str(result)
    assert parse_grid(body, dt.date(2026, 10, 22), "1603") == result


@pytest.mark.parametrize("body", ['<html>Login</html>', '''<option data-staff="1603">Owner</option>
<td class="cell booked staff-cell" data-reference="123" data-staff="1603"><span class="booking-time">unknown</span>'''])
def test_incomplete_grid_is_not_an_empty_day(body):
    with pytest.raises(ValueError):
        parse_grid(body, dt.date(2026, 10, 22), "1603")
