"""Report routes, mounted under /api/<version>/reports/.

The dashboard sits at /api/<version>/dashboard/ instead — it is not a report
of one module, it is the front page — so `config/urls.py` routes it directly.
"""

from django.urls import path

from .views import (
    AttendanceReportView,
    BeatReportView,
    CustomerReportView,
    ProductReportView,
    SalesReportView,
    SiteVisitReportView,
    TeamDirectoryView,
    TeamReportView,
    TrendsView,
    ReportTableView,
    VisitLogView,
)

app_name = 'reports'

urlpatterns = [
    path('sales/', SalesReportView.as_view(), name='sales'),
    path('attendance/', AttendanceReportView.as_view(), name='attendance'),
    path('beats/', BeatReportView.as_view(), name='beats'),
    path('site-visits/', SiteVisitReportView.as_view(), name='site-visits'),
    path('customers/', CustomerReportView.as_view(), name='customers'),
    path('products/', ProductReportView.as_view(), name='products'),
    # Per-day series for the charts, and per-person-day rows for the
    # table. Both are shapes the window reports above cannot produce.
    path('trends/', TrendsView.as_view(), name='trends'),
    path('table/', ReportTableView.as_view(), name='table'),
    # Per-person rollups for the management portal: who is in scope, and how
    # each of them compares across the window.
    path('employees/', TeamDirectoryView.as_view(), name='employees'),
    path('team/', TeamReportView.as_view(), name='team'),
    # The team's site visits as rows. `/site-visits/` is the rep's own day and
    # stays that way; this is the same records read from a desk.
    path('visit-log/', VisitLogView.as_view(), name='visit-log'),
]
