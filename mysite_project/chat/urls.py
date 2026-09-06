from django.urls import path

from . import views

app_name = 'chat'

urlpatterns = [
    # Conversations (giữ nguyên đường dẫn cũ)
    path('conversations/', views.ConversationListCreateView.as_view(), name='conversation-list'),
    path('conversations/<int:pk>/', views.ConversationDetailView.as_view(), name='conversation-detail'),
    path('conversations/<int:conversation_id>/messages/', views.MessageListView.as_view(), name='message-list'),

    # Quản lý phòng (mục 4.1)
    path('rooms/public/', views.PublicRoomsView.as_view(), name='room-public'),
    path('rooms/join/', views.RoomJoinView.as_view(), name='room-join'),
    path('rooms/<int:conversation_id>/leave/', views.RoomLeaveView.as_view(), name='room-leave'),
    path('rooms/<int:conversation_id>/members/', views.RoomMemberListView.as_view(), name='room-members'),
    path('rooms/<int:conversation_id>/members/<int:user_id>/remove/', views.RoomMemberRemoveView.as_view(), name='room-member-remove'),
    path('rooms/<int:conversation_id>/messages/search/', views.MessageSearchView.as_view(), name='message-search'),

    # Notifications
    path('notifications/', views.NotificationListView.as_view(), name='notification-list'),
    path('notifications/mark-read/', views.NotificationMarkReadView.as_view(), name='notification-mark-read'),
]
