from rss_post.read_rss import read_rss_items


def get_arbitrary_rss_data() -> str:
    return """
        <?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
            <channel>
                <title>Sample RSS Feed</title>
                <link>http://www.example.com</link>
                <description>Sample RSS Feed</description>
                <item>
                    <title>Item 1</title>
                    <link>http://www.example.com/item1</link>
                    <description>Item 1 description</description>
                </item>
                <item>
                    <title>Item 2</title>
                    <link>http://www.example.com/item2</link>
                    <description>Item 2 description</description>
                </item>
            </channel>
        </rss>
    """


def test_read_rss_items():
    rss_data = get_arbitrary_rss_data()
    items = read_rss_items(rss_data)
    assert len(items) == 2
    assert items[0]['title'] == 'Item 1'
    assert items[1]['title'] == 'Item 2'
