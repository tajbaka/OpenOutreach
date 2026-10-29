"""Offline DOM regression checks; no account, network or shared browser."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

from linkedin.feed_collection import extract_posts_from_page
from linkedin.feed_sampling import assert_feed_available


@pytest.fixture
def feed_page(monkeypatch):
    monkeypatch.setattr("linkedin.feed_collection.LINKEDIN_FEED_COLLECTION_ACTION_DELAY_MIN_SECONDS", 0)
    monkeypatch.setattr("linkedin.feed_collection.LINKEDIN_FEED_COLLECTION_ACTION_DELAY_MAX_SECONDS", 0)
    with sync_playwright() as pw:
        if not Path(pw.chromium.executable_path).exists():
            pytest.skip("Install Playwright Chromium for offline browser regression tests")
        browser = pw.chromium.launch(headless=True, executable_path=pw.chromium.executable_path)
        page = browser.new_page()
        page.set_content("""
            <div class="feed-shared-update-v2" id="post">
              <span class="update-components-actor__name">Alice Example</span>
              <span class="update-components-actor__sub-description">5h</span>
              <div class="update-components-text">Looking for FedRAMP support.</div>
              <button aria-label="Open control menu for post by Alice">Menu</button>
            </div>
            <script>
              window.menuClicks = 0;
              window.postUrn = 'urn:li:activity:123';
              document.querySelector('button').onclick = () => {
                window.menuClicks++;
                const menu = document.createElement('div');
                menu.setAttribute('role', 'menu');
                if (window.postUrn) {
                  const link = document.createElement('a');
                  link.href = 'https://www.linkedin.com/menu?targetUrn=' + encodeURIComponent(window.postUrn);
                  menu.append(link);
                }
                document.body.append(menu);
              };
              document.addEventListener('keydown', e => {
                if (e.key === 'Escape') document.querySelectorAll('[role=menu]').forEach(m => m.remove());
              });
            </script>
        """)
        try:
            yield page
        finally:
            browser.close()


def test_unchanged_card_reuses_url_but_recycled_card_gets_new_url(feed_page):
    first = extract_posts_from_page(feed_page)
    second = extract_posts_from_page(feed_page)
    assert first[0].activity_urn == second[0].activity_urn == "urn:li:activity:123"
    assert feed_page.evaluate("window.menuClicks") == 1

    feed_page.evaluate("""() => {
        document.querySelector('.update-components-text').textContent = 'A different FedRAMP post.';
        window.postUrn = 'urn:li:activity:456';
    }""")
    assert extract_posts_from_page(feed_page)[0].activity_urn == "urn:li:activity:456"
    assert feed_page.evaluate("window.menuClicks") == 2


def test_existing_activity_attribute_does_not_open_menu(feed_page):
    feed_page.evaluate("document.querySelector('#post').dataset.urn = 'urn:li:activity:789'")
    assert extract_posts_from_page(feed_page)[0].activity_urn == "urn:li:activity:789"
    assert feed_page.evaluate("window.menuClicks") == 0


def test_failed_menu_lookup_is_not_cached(feed_page):
    feed_page.evaluate("window.postUrn = ''")
    assert extract_posts_from_page(feed_page) == []
    feed_page.evaluate("window.postUrn = 'urn:li:activity:321'")
    assert extract_posts_from_page(feed_page)[0].activity_urn == "urn:li:activity:321"
    assert feed_page.evaluate("window.menuClicks") == 2


def test_sample_skips_missing_links_without_menu_clicks_or_scrolling(feed_page):
    assert extract_posts_from_page(feed_page, allow_menu_lookup=False, visible_only=True, max_nodes=20) == []
    assert feed_page.evaluate("window.menuClicks") == 0
    assert feed_page.evaluate("window.scrollY") == 0


def test_sample_extracts_visible_exact_links_only(feed_page):
    feed_page.evaluate("""() => {
        const post = document.querySelector('#post');
        post.dataset.urn = 'urn:li:activity:789';
        const hidden = post.cloneNode(true);
        hidden.id = 'hidden-post';
        hidden.dataset.urn = 'urn:li:activity:999';
        hidden.style.position = 'absolute';
        hidden.style.top = '3000px';
        document.body.append(hidden);
    }""")
    records = extract_posts_from_page(feed_page, allow_menu_lookup=False, visible_only=True, max_nodes=20)
    assert [record.activity_urn for record in records] == ['urn:li:activity:789']
    assert feed_page.evaluate("window.menuClicks") == 0


def test_current_sdui_header_identity_needs_no_menu(feed_page):
    feed_page.evaluate("""() => {
        const header = document.createElement('div');
        header.setAttribute('data-sdui-anchor-id', 'feed-header-urn:li:activity:123456789-0');
        document.querySelector('#post').prepend(header);
    }""")
    records = extract_posts_from_page(feed_page, allow_menu_lookup=False, visible_only=True, max_nodes=20)
    assert records[0].activity_urn == 'urn:li:activity:123456789'
    assert records[0].post_url == 'https://www.linkedin.com/feed/update/urn:li:activity:123456789/'
    assert feed_page.evaluate("window.menuClicks") == 0


def test_current_sdui_ugc_post_header_identity(feed_page):
    feed_page.evaluate("""() => {
        const header = document.createElement('div');
        header.setAttribute('data-sdui-anchor-id', 'feed-header-urn:li:ugcPost:987654321-0');
        document.querySelector('#post').prepend(header);
    }""")
    records = extract_posts_from_page(feed_page, allow_menu_lookup=False, visible_only=True, max_nodes=20)
    assert records[0].activity_urn == 'urn:li:ugcPost:987654321'
    assert records[0].post_url == 'https://www.linkedin.com/feed/update/urn:li:ugcPost:987654321/'
    assert feed_page.evaluate('window.menuClicks') == 0


@pytest.mark.parametrize('nested', [False, True])
def test_sample_scrolls_part_of_viewport_smoothly(feed_page, monkeypatch, nested):
    from linkedin.feed_sampling import _scroll_sample_page
    from unittest.mock import Mock
    choose = Mock(return_value=0.9)
    monkeypatch.setattr('linkedin.feed_sampling.random.uniform', choose)
    feed_page.set_content('''
        <style>body {margin: 0} main {height: 600px} .content {height: 5000px}</style>
        <main id="workspace"><div class="content">Synthetic feed</div></main>
    ''')
    if nested:
        feed_page.evaluate("document.querySelector('main').style.overflowY = 'auto'")
    else:
        feed_page.evaluate("document.querySelector('main').style.height = 'auto'")
    height = feed_page.evaluate("document.querySelector('main').clientHeight" if nested else 'window.innerHeight')
    offset = "document.querySelector('main').scrollTop" if nested else 'window.scrollY'
    _scroll_sample_page(feed_page)
    # A smooth scroll does not jump to its final position in the issuing task.
    assert feed_page.evaluate(offset) < height * 0.9 - 5
    feed_page.wait_for_function(
        f'() => Math.abs(({offset}) - {int(height * 0.9)}) <= 2', timeout=3000,
    )
    assert abs(feed_page.evaluate(offset) - int(height * 0.9)) <= 2
    choose.assert_called_once_with(0.8, 1.0)


def install_reaction_bindings(page, bindings):
    page.evaluate("""bindings => {
        const card = document.querySelector('#post');
        const boundary = {return: {memoizedProps: {
            selectedReactionStateBindingKey: {key: {value: {id: 'reactionState-urn:li:activity:999999'}}}
        }}};
        card.__reactFiber$fixture = boundary;
        for (const old of card.querySelectorAll('.test-reaction')) old.remove();
        for (const id of bindings) {
            const button = document.createElement('button');
            button.className = 'test-reaction'; button.textContent = 'Like';
            button.__reactFiber$fixture = {return: {
                memoizedProps: {selectedReactionStateBindingKey: {key: {value: {id}}}},
                return: boundary,
            }};
            card.append(button);
        }
    }""", bindings)


def test_sdui_reaction_binding_resolves_own_post_without_clicks(feed_page):
    install_reaction_bindings(feed_page, [
        'reactionState-urn:li:ugcPost:12345',
        'reactionState-urn:li:comment:(activity:56789,98765)',
    ])
    feed_page.evaluate("""() => {
        const quote = document.createElement('a');
        quote.href = 'https://www.linkedin.com/feed/update/urn:li:activity:77777/';
        quote.textContent = 'A post mentioned in the body';
        document.querySelector('#post').append(quote);
    }""")
    diagnostics = {}
    records = extract_posts_from_page(feed_page, allow_menu_lookup=False, diagnostics=diagnostics)
    assert records[0].activity_urn == 'urn:li:ugcPost:12345'
    assert records[0].post_url == 'https://www.linkedin.com/feed/update/urn:li:ugcPost:12345/'
    assert diagnostics['cards'][0]['status'] == 'extracted'
    assert feed_page.evaluate('window.menuClicks') == 0


def test_quoted_post_link_never_becomes_card_identity(feed_page):
    feed_page.evaluate("""() => {
        const quote = document.createElement('a');
        quote.href = 'https://www.linkedin.com/feed/update/urn:li:ugcPost:77777/';
        quote.textContent = 'Black Mirror is officially real now A YouTuber 3d later';
        document.querySelector('#post').append(quote);
    }""")
    diagnostics = {}
    assert extract_posts_from_page(feed_page, allow_menu_lookup=False, diagnostics=diagnostics) == []
    assert diagnostics['cards'][0]['status'] == 'unverified_post_link'


def test_own_timestamp_permalink_is_card_identity(feed_page):
    feed_page.evaluate("""() => {
        const stamp = document.querySelector('.update-components-actor__sub-description');
        const link = document.createElement('a');
        link.href = 'https://www.linkedin.com/feed/update/urn:li:activity:4242/';
        stamp.before(link);
        link.append(stamp);
    }""")
    records = extract_posts_from_page(feed_page, allow_menu_lookup=False)
    assert records[0].activity_urn == 'urn:li:activity:4242'


def test_reaction_binding_outranks_header_anchor(feed_page):
    install_reaction_bindings(feed_page, ['reactionState-urn:li:ugcPost:12345'])
    feed_page.evaluate("""() => {
        const header = document.createElement('div');
        header.setAttribute('data-sdui-anchor-id', 'feed-header-urn:li:activity:123456789-0');
        document.querySelector('#post').prepend(header);
    }""")
    records = extract_posts_from_page(feed_page, allow_menu_lookup=False)
    assert records[0].activity_urn == 'urn:li:ugcPost:12345'


def install_double_buffered_card(page, current_id, previous_id=None):
    # React keeps a current and a previous fiber for each node; a descendant
    # may climb to either copy of the card's fiber.
    page.evaluate("""([currentId, previousId]) => {
        const card = document.querySelector('#post');
        const binding = id => ({selectedReactionStateBindingKey: {key: {value: {id}}}});
        const outside = {memoizedProps: binding('reactionState-urn:li:activity:999999')};
        const current = {stateNode: card, return: outside};
        const previous = {stateNode: card, return: outside, alternate: current};
        current.alternate = previous;
        card.__reactFiber$fixture = current;
        const reaction = {memoizedProps: binding(currentId), return: previous};
        if (previousId) reaction.alternate = {memoizedProps: binding(previousId), return: current, alternate: reaction};
        const button = document.createElement('button');
        button.textContent = 'Like';
        button.__reactFiber$fixture = {return: reaction};
        card.append(button);
    }""", [current_id, previous_id])


def test_reaction_walk_stops_at_either_copy_of_the_card_fiber(feed_page):
    install_double_buffered_card(feed_page, 'reactionState-urn:li:ugcPost:12345')
    records = extract_posts_from_page(feed_page, allow_menu_lookup=False)
    assert records[0].activity_urn == 'urn:li:ugcPost:12345'


def test_reaction_binding_that_changed_between_renders_is_not_trusted(feed_page):
    install_double_buffered_card(
        feed_page, 'reactionState-urn:li:activity:22222', 'reactionState-urn:li:activity:11111',
    )
    diagnostics = {}
    assert extract_posts_from_page(feed_page, allow_menu_lookup=False, diagnostics=diagnostics) == []
    assert diagnostics['cards'][0]['status'] == 'ambiguous_identity'


@pytest.mark.parametrize('bindings', [
    ['reactionState-urn:li:comment:(activity:11111,22222)'],
    ['reactionState-urn:li:activity:11111', 'reactionState-urn:li:activity:22222'],
    [],
])
def test_comment_ambiguous_and_other_card_bindings_do_not_invent_identity(feed_page, bindings):
    install_reaction_bindings(feed_page, bindings)
    diagnostics = {}
    assert extract_posts_from_page(feed_page, allow_menu_lookup=False, diagnostics=diagnostics) == []
    assert diagnostics['cards'][0]['status'] == ('ambiguous_identity' if len(bindings) > 1 else 'missing_identity')
    assert feed_page.evaluate('window.menuClicks') == 0


def test_recycled_card_reloads_reaction_identity(feed_page):
    install_reaction_bindings(feed_page, ['reactionState-urn:li:activity:11111'])
    assert extract_posts_from_page(feed_page)[0].activity_urn == 'urn:li:activity:11111'
    install_reaction_bindings(feed_page, ['reactionState-urn:li:activity:22222'])
    assert extract_posts_from_page(feed_page)[0].activity_urn == 'urn:li:activity:22222'
    assert feed_page.evaluate('window.menuClicks') == 0


def test_recommendations_count_separately_from_unresolved_posts(feed_page):
    feed_page.evaluate("""() => {
        const module = document.createElement('div');
        module.setAttribute('role', 'listitem');
        module.setAttribute('componentkey', 'recommendation-module');
        module.innerText = 'Feed post\\nRecommended for you\\nCompany A\\nFollow\\nCompany B\\nFollow';
        document.body.append(module);
    }""")
    diagnostics = {}
    extract_posts_from_page(feed_page, allow_menu_lookup=False, diagnostics=diagnostics)
    assert [card['status'] for card in diagnostics['cards']] == ['missing_identity', 'non_post_module']


def on_feed_url(page):
    # Pass the sampler's URL check while the offline document stays under test.
    return SimpleNamespace(url='https://www.linkedin.com/feed/', evaluate=page.evaluate)


SECURITY_POST = 'Verify your identity controls, unusual activity alerts and security verification for FedRAMP.'


@pytest.mark.parametrize('container', [
    "document.querySelector('#post')",
    """(() => {
        const card = document.createElement('div');
        card.setAttribute('role', 'listitem'); card.innerText = 'Feed post\\nAlice Example';
        return document.body.appendChild(card);
    })()""",
    """(() => {
        const rail = document.createElement('aside'); rail.innerText = 'LinkedIn News';
        return document.body.appendChild(rail);
    })()""",
])
def test_security_topics_in_feed_content_are_not_checkpoints(feed_page, container):
    feed_page.evaluate(f"""text => {{
        const paragraph = document.createElement('p'); paragraph.textContent = text;
        ({container}).append(paragraph);
    }}""", SECURITY_POST)
    assert_feed_available(on_feed_url(feed_page))


@pytest.mark.parametrize('markup', [
    '<div hidden>Security verification</div>',
    '<code style="display: none">{"text": "verify your identity"}</code>',
    '<script type="application/json">{"text": "unusual activity"}</script>',
])
def test_hidden_page_data_is_not_a_checkpoint(feed_page, markup):
    feed_page.evaluate("markup => document.body.insertAdjacentHTML('beforeend', markup)", markup)
    assert_feed_available(on_feed_url(feed_page))


@pytest.mark.parametrize('markup,keep_feed', [
    ('<div role="dialog"><h2>Let’s do a quick security verification</h2></div>', True),
    ('<form action="/checkpoint/challenge/verify"><button>Continue</button></form>', True),
    ('<iframe src="about:blank#captcha"></iframe>', True),
    ('<main><h1>Your account has been <b>restricted</b></h1></main>', False),
    ('<main><p>ERR_INTERNET_DISCONNECTED</p></main>', False),
])
def test_page_level_checkpoint_still_holds(feed_page, markup, keep_feed):
    feed_page.evaluate("""([markup, keepFeed]) => {
        if (!keepFeed) document.querySelector('#post').remove();
        document.body.insertAdjacentHTML('beforeend', markup);
    }""", [markup, keep_feed])
    with pytest.raises(RuntimeError):
        assert_feed_available(on_feed_url(feed_page))


def test_job_recommendation_module_is_not_a_post(feed_page):
    feed_page.evaluate("""() => {
        const module = document.createElement('div');
        module.setAttribute('role', 'listitem');
        module.setAttribute('componentkey', 'job-recommendations');
        module.innerText = 'Feed post\\nJobs recommended for you\\nEngineer roles';
        for (const id of ['111','222']) {
            const link = document.createElement('a');
            link.href = 'https://www.linkedin.com/jobs/search-results/?currentJobId=' + id;
            link.textContent = 'Engineer'; module.append(link);
        }
        document.body.append(module);
    }""")
    diagnostics = {}
    extract_posts_from_page(feed_page, allow_menu_lookup=False, diagnostics=diagnostics)
    assert [card['status'] for card in diagnostics['cards']] == ['missing_identity', 'non_post_module']
