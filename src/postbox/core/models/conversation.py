# conversation.py
#
# Grouping of related emails into one thread. Real threading logic lands in
# Phase 8 — this is a placeholder so the target module layout exists now.
#
# SPDX-License-Identifier: GPL-3.0-or-later

from gi.repository import GObject


class Conversation(GObject.Object):
    __gtype_name__ = "PostboxConversation"

    def __init__(self, id: int) -> None:
        super().__init__()
        self.id: int = id
