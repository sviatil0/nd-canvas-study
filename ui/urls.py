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
]
