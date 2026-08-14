"""Permission classes for the dashboard and the reports.

No new codenames. Two that `accounts.BusinessPermission` already registers do
the whole job:

* `view_reports`      — may open a dashboard or a report at all
* `view_team_reports` — the figures cover the organisation rather than just
                        the person asking

That second one is a scope, not a gate: without it the endpoints still answer,
they just answer about you. A 403 there would be the wrong shape of "no" —
a field executive's own figures are exactly what a field executive should see.
"""

from rest_framework.permissions import BasePermission


class CanViewReports(BasePermission):
    """Everything in this module is read-only, so there is one rule."""

    message = 'Your role does not include reports.'

    def has_permission(self, request, view):
        return bool(
            request.user and request.user.has_perm('accounts.view_reports')
        )


class CanViewTeamReports(BasePermission):
    """For the endpoints that only make sense across a team.

    Not used by the dashboard or by the six reports, which scope themselves
    instead. Kept here because the distinction is the one a future
    per-employee league table will need, and the alternative is inventing it
    again under pressure.
    """

    message = "Your role does not include other people's figures."

    def has_permission(self, request, view):
        return bool(
            request.user and request.user.has_perm('accounts.view_team_reports')
        )
