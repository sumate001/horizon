

def test_stats_reports_a_failure_rate_not_only_a_lifetime_total():
    """articles_by_status only grows, so it reads the same on a good day as on a
    bad one — a five-digit "failed" count sat on the dashboard while extraction
    was losing half the news."""
    from horizon.services.api import Stats

    assert "failure_rate_24h" in Stats.model_fields
    assert "articles_failed_24h" in Stats.model_fields
