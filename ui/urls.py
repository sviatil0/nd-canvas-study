from django.urls import path

from . import views

app_name = "ui"

urlpatterns = [
    path("", views.index, name="index"),
    path("auth/", views.auth_login, name="auth"),
    path("auth/check/", views.auth_check, name="auth_check"),
    path("courses/refresh/", views.courses_refresh, name="courses_refresh"),
    path("course/<int:cid>/sync/", views.sync_course, name="sync_course"),
    path("course/<int:cid>/", views.course_detail, name="course_detail"),
    path("course/<int:cid>/build/", views.build_bundle, name="build_bundle"),
    path("course/<int:cid>/analyze/", views.analyze_course, name="analyze_course"),
    path("course/<int:cid>/bundle/<str:cat>/", views.view_bundle, name="view_bundle"),
    path("course/<int:cid>/file/<path:rel>/", views.serve_file, name="serve_file"),
    path("course/<int:cid>/index/", views.build_index, name="build_index"),
    path("course/<int:cid>/ask/", views.ask, name="ask"),
    path("course/<int:cid>/topic/<str:topic>/", views.topic_detail, name="topic_detail"),
    path("course/<int:cid>/snippet/<path:rel>/<int:page>/", views.snippet, name="snippet"),
    path("course/<int:cid>/solve/", views.solve_problem, name="solve_problem"),
    path("course/<int:cid>/topic/<str:topic>/summary/build/", views.build_summary, name="build_summary"),
    path("course/<int:cid>/graph/", views.graph_view, name="graph_view"),
    path("course/<int:cid>/path/", views.path_view, name="path_view"),
    path("course/<int:cid>/graph.png", views.graph_png, name="graph_png"),
    path("course/<int:cid>/graph/render/", views.render_graph_png, name="render_graph_png"),
    path("course/<int:cid>/topic/<str:topic>/followup/", views.followup, name="followup"),
    path("course/<int:cid>/jobs/", views.jobs_view, name="jobs"),
    path("course/<int:cid>/jobs/status/", views.jobs_status, name="jobs_status"),
    path("course/<int:cid>/sync/status/", views.sync_status, name="sync_status"),
    path("course/<int:cid>/sync/view/", views.sync_view, name="sync_view"),
    path("course/<int:cid>/progress/", views.progress_get, name="progress_get"),
    path("course/<int:cid>/progress/toggle/", views.progress_toggle, name="progress_toggle"),
    path("course/<int:cid>/mock/", views.mock_exam, name="mock_exam"),
    path("course/<int:cid>/grade/", views.grade_answer, name="grade_answer"),
    path("course/<int:cid>/formulas/", views.formulas, name="formulas"),
    path("course/<int:cid>/likelihood/", views.likelihood, name="likelihood"),
    path("course/<int:cid>/likelihood/build/", views.build_likelihood, name="build_likelihood"),
]
