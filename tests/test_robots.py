from media_resource_manager.crawler.robots import RobotsPolicy


def test_allow_all_when_empty():
    p = RobotsPolicy("")
    assert p.allowed("/anything")


def test_disallow_root_blocks():
    p = RobotsPolicy("User-agent: *\nDisallow: /")
    assert not p.allowed("/gallery")


def test_specific_allow_overrides():
    p = RobotsPolicy(
        "User-agent: *\nDisallow: /\nAllow: /public\n")
    assert not p.allowed("/private")
    assert p.allowed("/public/x.jpg")


def test_glob_and_dollar():
    p = RobotsPolicy("User-agent: *\nDisallow: /*.php$\n")
    assert not p.allowed("/page.php")
    assert p.allowed("/page.html")


def test_crawl_delay_parsed():
    p = RobotsPolicy("User-agent: *\nCrawl-Delay: 2\n")
    assert p.crawl_delay == 2.0


def test_named_bot_group_does_not_leak_to_unnamed():
    """具名 bot 的禁令不得漏进 * 组误伤普通 UA（wp.com Photon CDN 真实场景）。"""
    text = (
        "User-agent: GPTBot\nDisallow: /\n\n"
        "User-agent: CCBot\nDisallow: /\n"
    )
    p = RobotsPolicy(text)
    assert "*" not in p.groups
    assert p.allowed("/pic.4khd.com/x.jpg")                        # 未具名 UA 放行
    assert not RobotsPolicy(text, "GPTBot").allowed("/anything")   # 具名 bot 仍被禁


def test_star_group_and_named_group_combine_correctly():
    text = ("User-agent: GPTBot\nDisallow: /tmp\n\n"
            "User-agent: *\nDisallow: /private\n")
    assert not RobotsPolicy(text, "GPTBot").allowed("/tmp/a")      # 具名组禁令生效
    assert RobotsPolicy(text, "GPTBot").allowed("/public/a")       # 具名组之外放行
    assert not RobotsPolicy(text).allowed("/private/a")            # * 组对普通 UA 生效
    assert RobotsPolicy(text).allowed("/public/a")


def test_orphan_rules_before_user_agent_go_to_star():
    """首组 user-agent 之前出现的规则按惯例归入 * 组。"""
    text = "Disallow: /secret\n\nUser-agent: GPTBot\nDisallow: /tmp\n"
    p = RobotsPolicy(text)
    assert "*" in p.groups
    assert not p.allowed("/secret/x")
    assert not RobotsPolicy(text, "GPTBot").allowed("/tmp/x")
    assert RobotsPolicy(text, "GPTBot").allowed("/other")
