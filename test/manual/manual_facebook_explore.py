from playwright.sync_api import sync_playwright


def explore_facebook():
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp("http://localhost:9222")
            context = browser.contexts[0]
            page = context.new_page()
            page.goto("https://www.facebook.com/zuck", timeout=60000)
            page.wait_for_selector('div[role="article"]', timeout=30000)
            
            # Extract basic post info
            posts = page.evaluate('''() => {
                const results = [];
                const articles = document.querySelectorAll('div[role="article"]');
                for(let i=0; i<Math.min(articles.length, 3); i++) {
                    const article = articles[i];
                    results.push({
                        html: article.innerHTML.substring(0, 1000), // First 1000 chars of HTML
                        hasVideo: !!article.querySelector('video'),
                        hasImage: !!article.querySelector('img'),
                        links: Array.from(article.querySelectorAll('a[href]')).map(a => a.href)
                    });
                }
                return results;
            }''')
            
            print("Posts found:", len(posts))
            for i, p in enumerate(posts):
                print(f"Post {i+1}: Video={p['hasVideo']}, Image={p['hasImage']}")
                
            page.close()
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    explore_facebook()
