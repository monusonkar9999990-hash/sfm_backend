"""Attendance routes, mounted under /api/<version>/attendance/."""

from django.urls import path

from .views import (
    AttendanceHistoryView,
    CheckInView,
    CheckOutView,
    TodayAttendanceView,
)

app_name = 'attendance'

urlpatterns = [
    path('check-in/', CheckInView.as_view(), name='check-in'),
    path('check-out/', CheckOutView.as_view(), name='check-out'),
    path('today/', TodayAttendanceView.as_view(), name='today'),
    path('', AttendanceHistoryView.as_view(), name='history'),
]
