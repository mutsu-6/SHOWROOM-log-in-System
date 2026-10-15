from showroom_keeper.directory import collect_platform_rooms, rooms_from_onlives


def test_onlives_dedupes_rooms_listed_in_several_genres():
    found = {}
    rooms_from_onlives({
        "onlives": [
            {"genre_id": 0, "lives": [{"room_id": 1, "room_url_key": "a"}, {"room_id": "2", "room_url_key": "b"}]},
            {"genre_id": 102, "lives": [{"room_id": 1, "room_url_key": "a"}, {"room_id": 0}, "nope"]},
        ]
    }, found)
    assert found == {1: "a", 2: "b"}


async def test_collect_platform_rooms_uses_live_upcoming_and_open_events():
    pages = {
        "/api/live/onlives": {"onlives": [{"lives": [{"room_id": 1, "room_url_key": "live"}]}]},
        "/api/search/room_genres": [{"genre_id": "102", "genre_name": "アイドル"}, {"genre_id": "0"}],
        "/api/genre_ranking/room_genre": {"room_genre": [
            {"genre_id": 200, "name": "ライバー"},
            {"genre_id": "102", "name": "アイドル"},
        ]},
        "/api/live/upcoming?genre_id=102": {"upcomings": [{"room_id": 2, "room_url_key": "soon"}]},
        "/api/live/upcoming?genre_id=200": {"upcomings": []},
        "/api/event/search?page=1": {"event_list": [
            {"event_url_key": "open", "is_closed": False, "is_public": True},
            {"event_url_key": "done", "is_closed": True, "is_public": True},
            {"event_url_key": "hidden", "is_closed": False, "is_public": False},
            {"event_url_key": "bad/key", "is_closed": False, "is_public": True},
        ], "next_page": None},
        "/api/event/open/rooms?page=1": {"rooms": [
            {"room_id": 3, "room_url_key": "joined"},
            {"room_id": 1, "room_url_key": "live"},
        ], "next_page": None},
    }

    calls = []

    async def get_json(path):
        calls.append(path)
        return pages[path]

    assert sorted(await collect_platform_rooms(get_json)) == [1, 2, 3]
    assert calls.count("/api/live/upcoming?genre_id=102") == 1
    assert "/api/event/bad/key/rooms?page=1" not in calls
