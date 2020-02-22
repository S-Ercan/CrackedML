"""
Script to retrieve news articles from NU.nl, insert them into a MongoDB database,
and update them with the number of comments they have received.
"""
import errno
import logging
import lxml.html as html_parser
import os
import urllib.request
import re

from datetime import datetime, timedelta
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from urllib.error import URLError

db_name = 'nu'
collection_name = 'articles'
db = Database(MongoClient(), db_name)
collection = Collection(db, collection_name)

base_url = 'http://www.nu.nl'


def collect_articles():
    """ Retrieves articles that aren't yet in the database and saves them to the database.
    """
    retrieved_urls = get_retrieved_urls()
    front_page = get_front_page()
    articles = get_articles(front_page, retrieved_urls)
    save_articles(articles)


def get_retrieved_urls():
    """
    :return: list of URLs of all articles in the database
    """
    retrieved_articles = collection.find({}, {'news_url': 1})
    retrieved_urls = [article['news_url'] for article in retrieved_articles]
    print_and_log_message('Found %d URLs already retrieved...\n' % len(retrieved_urls))
    return retrieved_urls


def get_front_page():
    """
    :return: page at URL 'base_url'
    """
    print_and_log_message("Checking for articles on %s..." % base_url)
    try:
        return download_page(base_url)
    except urllib.error.URLError:
        print_and_log_message('Could not access %s.' % base_url, level=logging.WARNING)
        exit()


def get_articles(page, retrieved_urls):
    """
    :param page: page containing URLs of news articles
    :param retrieved_urls: URLs of articles already in the database
    :return: Retrieves all articles on 'page' the URLs of which are not in 'retrieved_urls'.
    """
    num_links_processed = 0
    articles = []
    # Article URLs are contained in <a> elements inside <div class="column-content">
    url_elements = page.xpath('//div[@class="column-content"]//a')

    for url_element in url_elements:
        values = url_element.values()
        if len(values) == 2:
            url = values[0]
            # Check whether URL belongs to a news item
            if 'advertorial' not in url and re.match('/.+/\d+/.+', url):
                num_links_processed += 1
                article_url = '%s%s' % (base_url, url)
                # Skip if already processed
                if article_url not in retrieved_urls:
                    article = process_article('%s%s' % (base_url, url))
                    if article is not None:
                        articles.append(article)

    print_and_log_message("Retrieved %d new articles, skipped %d existing ones.\n" %
                          (len(articles), num_links_processed - len(articles)))
    return articles


def process_article(url):
    """
    :param url: URL of article to process
    :return: dict containing article contents
    """
    print('Retrieving article from %s...' % url)
    # Retrieve article
    try:
        article = download_page(url)
    except URLError:
        print_and_log_message('Could not retrieve article from %s.' % url, level=logging.WARNING)
        return None
    # Extract contents
    article_contents = extract_article_contents(article)
    if article_contents is not None:
        article_contents['news_url'] = url
    return article_contents


def extract_article_contents(article):
    """
    :param article: article to extract contents of
    :return: dict containing article contents
    """
    # Extract publication date
    published = article.find('//span[@class="published"]//span[@class="small"]').text.strip()
    published_date = datetime.strptime(published, '%d-%m-%y %H:%M')
    # Extract title
    title = article.find('//div[@class="title"]//h1[@class="fluid"]').text.strip()
    # Extract article text
    text = extract_article_text(article)
    # Extract the NUjij link used for commenting
    comments_url = article.find(
        '//ul[@class="social-buttons"]//li[@class="nujij"]//a[@class="tracksocial"]'
    ).attrib['href']

    # Check whether the comments URL is valid
    if comments_url.startswith('http://www.nujij.nl/jij.lynkx/?u=http') and 'slideshow' in comments_url:
        print('Could not retrieve comments URL.')
        return None
    else:
        return dict(
            published=published_date,
            comments_url=comments_url,
            title=title,
            text=text,
            num_comments=None
        )


def extract_article_text(article):
    """
    :param article: article to extract text of
    :return: string containing all text of article
    """
    # Extract excerpt
    text = article.find('//div[@class="item-excerpt"]').text.strip()
    # Extract text
    text_elements = article.xpath('//div[@class="zone"]//div[@class="block-content"]//p')
    for text_element in text_elements:
        element_text = text_element.text
        if element_text:
            text += element_text.strip()
    return text


def get_number_of_comments():
    """
    Retrieves the number of comments for each article published at least 24 hours ago.
    Updates the corresponding article document with the retrieved number of comments.
    """
    date = datetime.now() - timedelta(days=1)
    # Get articles older than 24 hours which haven't yet had their number of comments updated
    articles = collection.find({'published': {'$lt': date}, 'num_comments': None})
    print_and_log_message('Updating number of comments for %d articles...' % articles.count())

    num_comments_updated = 0
    for article in articles:
        comments_url = article['comments_url']
        # Retrieve comments page
        print('Retrieving comments from %s...' % comments_url)
        try:
            comments_page = download_page(comments_url)
        except URLError:
            print_and_log_message('Could not retrieve comments page from %s.' % comments_url, level=logging.WARNING)
            continue
        # Search for element containing number of comments
        comments_element = comments_page.find('//span[@class="bericht-reacties"]')
        if comments_element is None:
            print_and_log_message('Could not find comments, deleting article with id %s...' %
                                  article['_id'], level=logging.WARNING)
            collection.delete_one({'_id': article['_id']})
            continue

        # Update article with the number of comments it has received
        comments_text = comments_element.text.strip()
        num_comments = int(comments_text.split(' ')[0])
        collection.update_one({'_id': article['_id']}, {'$set': {'num_comments': num_comments}})
        num_comments_updated += 1

    if num_comments_updated > 0:
        print_and_log_message('Updated number of comments for %d articles.' % num_comments_updated)


def download_page(url):
    """
    :param url: URL of page to retrieve
    :return: web page at URL url
    """
    return html_parser.parse(urllib.request.urlopen(url))


def save_articles(articles):
    """
    Inserts articles into the database.
    :param articles: list of articles
    """
    if isinstance(articles, list) and len(articles) > 0:
        collection.insert_many(articles)
        print_and_log_message("Inserted %d articles into '%s.%s'.\n" % (len(articles), db_name, collection_name))


def get_log_file_name():
    """
    :return: absolute path of log file to use, resolves to '/absolute/path/to/project/NewsClassification/log/log.txt'.
    """
    file_dir = os.path.dirname(os.path.realpath(__file__))
    project_dir = os.path.abspath(os.path.join(file_dir, '..'))
    log_dir = os.path.join(project_dir, 'log')
    if not os.path.exists(log_dir):
        try:
            os.makedirs(log_dir)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
    return os.path.join(log_dir, 'log.txt')


def print_and_log_message(message, level=logging.INFO):
    """
    :param message: message to print and log
    :param level: level at which to log message
    """
    print(message)
    message = '%s: %s' % (datetime.now().strftime('%d-%m-%Y %H:%M:%S'), message)
    logging.log(level=level, msg=message)


if __name__ == '__main__':
    # Initialize logging
    log_file = get_log_file_name()
    logging.basicConfig(filename=log_file, level=logging.DEBUG)
    # Retrieve articles and insert them into the database
    collect_articles()
    # For articles that are old enough, update the number of comments they have received
    get_number_of_comments()
