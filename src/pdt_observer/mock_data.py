from __future__ import annotations

from pdt_observer.models import InvestigationTask, ObservationType, PlaceRecord, SourceDocument

BLUE_LANTERN_QUOTE = (
    "Officials said 17 people were inside the Blue Lantern restaurant when crews "
    "arrived at approximately 9:10 p.m."
)

MOCK_DOCUMENTS: tuple[SourceDocument, ...] = (
    SourceDocument(
        document_id="milltown-blue-lantern-response",
        title="Crews respond to Blue Lantern kitchen fire",
        source_url="https://news.example.invalid/milltown/blue-lantern-response",
        locality="Milltown",
        country="US",
        tags=("milltown", "people_present", "blue_lantern"),
        text=(
            "Emergency crews were dispatched to the Blue Lantern on Friday night. "
            f"{BLUE_LANTERN_QUOTE} "
            "No serious injuries were reported, and the fire marshal said the cause "
            "remains under review."
        ),
    ),
    SourceDocument(
        document_id="milltown-harbor-hall-crowd",
        title="Harbor Hall event paused after alarm",
        source_url="https://news.example.invalid/milltown/harbor-hall-alarm",
        locality="Milltown",
        country="US",
        tags=("milltown", "people_present", "harbor_hall", "ambiguous"),
        text=(
            "Authorities reported 22 people were inside the Harbor Hall venue when "
            "officers arrived shortly after 8 p.m. The alarm was later traced to a "
            "faulty sensor."
        ),
    ),
    SourceDocument(
        document_id="milltown-profile-no-count",
        title="Blue Lantern marks ten years downtown",
        source_url="https://news.example.invalid/milltown/blue-lantern-profile",
        locality="Milltown",
        country="US",
        tags=("milltown", "profile", "blue_lantern"),
        text=(
            "The Blue Lantern opened at 17 Oak Street in 2014. Its owner said the "
            "restaurant plans a neighborhood dinner next month."
        ),
    ),
    SourceDocument(
        document_id="milltown-number-traps",
        title="Milltown council reviews Oak Street repairs",
        source_url="https://news.example.invalid/milltown/oak-street-repairs",
        locality="Milltown",
        country="US",
        tags=("milltown", "numbers", "nonqualifying"),
        text=(
            "Firefighters responded to 17 Oak Street on May 4. Two people were "
            "treated for smoke inhalation outside the building, and the construction "
            "estimate was $2 million."
        ),
    ),
)

MOCK_PLACES: tuple[PlaceRecord, ...] = (
    PlaceRecord(
        place_id="place-blue-lantern-milltown",
        name="Blue Lantern",
        locality="Milltown",
        country="US",
        latitude=41.24831,
        longitude=-72.67344,
        method="mock_unique_name_locality",
    ),
    PlaceRecord(
        place_id="place-harbor-hall-north",
        name="Harbor Hall",
        locality="Milltown",
        country="US",
        latitude=41.25101,
        longitude=-72.67002,
        method="mock_name_locality_ambiguous",
    ),
    PlaceRecord(
        place_id="place-harbor-hall-south",
        name="Harbor Hall",
        locality="Milltown",
        country="US",
        latitude=41.24012,
        longitude=-72.68111,
        method="mock_name_locality_ambiguous",
    ),
)

DEFAULT_MILLTOWN_TASK = InvestigationTask(
    task_id="task-milltown-blue-lantern",
    locality="Milltown",
    country="US",
    observation_type=ObservationType.PEOPLE_PRESENT,
    maximum_agent_turns=6,
)
