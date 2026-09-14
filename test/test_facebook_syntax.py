import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.platforms.facebook.keyword_search import run_facebook_keyword_search_spider
from src.platforms.facebook.profile_works import run_facebook_profile_works_spider

def test_facebook_spiders_are_callable():
    assert callable(run_facebook_keyword_search_spider)
    assert callable(run_facebook_profile_works_spider)
