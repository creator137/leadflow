from bs4 import BeautifulSoup

from app.sources.phrase_search.service import _organization_metadata


def test_extracts_public_organization_json_ld() -> None:
    soup = BeautifulSoup(
        """
        <script type="application/ld+json">
        {"@type":"Organization","name":"Фабрика","telephone":"+7 495 123-45-67",
         "address":{"postalCode":"142400","addressLocality":"Ногинск","streetAddress":"ул. Ленина, 1"}}
        </script>
        """,
        "html.parser",
    )

    assert _organization_metadata(soup) == {
        "name": "Фабрика",
        "phone": "+7 495 123-45-67",
        "city": "Ногинск",
        "address": "142400, Ногинск, ул. Ленина, 1",
    }
