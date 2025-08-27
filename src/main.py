import os
from typing import List, Dict
from mcp.server.fastmcp import FastMCP
import aiohttp
from bs4 import BeautifulSoup
import json
from urllib.parse import urljoin, urlparse
import time
from dataclasses import dataclass

@dataclass
class CachedPage:
    title: str
    content: str
    url: str
    site: str
    timestamp: float
    ttl: float = 3600  # 1 hour default TTL
    
    @property
    def is_expired(self) -> bool:
        return time.time() > self.timestamp + self.ttl

class DocumentationIndexer:
    def __init__(self):
        self.cached_pages = {}  # URL -> CachedPage
        self.search_cache = {}  # query -> (results, timestamp)
        self.base_urls = {
            'cortex_cloud': 'https://docs-cortex.paloaltonetworks.com/p/Cortex+CLOUD',
            'cortex_api': 'https://docs-cortex.paloaltonetworks.com/r/Cortex-Cloud-Platform-APIs/Cortex-Cloud-APIs',
        }
        self.search_cache_ttl = 300  # 5 minutes for search results
    
    async def index_site(self, site_name: str, max_pages: int = 100):
        """Index documentation from a specific site"""
        if site_name not in self.base_urls:
            raise ValueError(f"Unknown site: {site_name}")
        
        base_url = self.base_urls[site_name]
        visited_urls = set()
        urls_to_visit = [base_url]
        pages_indexed = 0
        
        async with aiohttp.ClientSession() as session:
            while urls_to_visit and pages_indexed < max_pages:
                url = urls_to_visit.pop(0)
                
                if url in visited_urls:
                    continue
                    
                visited_urls.add(url)
                
                try:
                    async with session.get(url, timeout=10) as response:
                        if response.status == 200:
                            content = await response.text()
                            soup = BeautifulSoup(content, 'html.parser')
                            
                            # Extract page content
                            title = soup.find('title')
                            title_text = title.text.strip() if title else url
                            
                            # Remove script and style elements
                            for script in soup(["script", "style"]):
                                script.decompose()
                            
                            # Get text content
                            text_content = soup.get_text()
                            lines = (line.strip() for line in text_content.splitlines())
                            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
                            text = ' '.join(chunk for chunk in chunks if chunk)
                            
                            # Store in cache
                            self.cached_pages[url] = CachedPage(
                                title=title_text,
                                content=text[:5000],  # Limit content length
                                url=url,
                                site=site_name,
                                timestamp=time.time()
                            )
                            
                            pages_indexed += 1
                            
                            # Find more links to index
                            if pages_indexed < max_pages:
                                links = soup.find_all('a', href=True)
                                for link in links:
                                    href = link['href']
                                    full_url = urljoin(url, href)
                                    
                                    # Only index URLs from the same domain
                                    if urlparse(full_url).netloc == urlparse(base_url).netloc:
                                        if full_url not in visited_urls and full_url not in urls_to_visit:
                                            urls_to_visit.append(full_url)
                                
                except Exception as e:
                    print(f"Error indexing {url}: {e}")
                    continue
        
        return pages_indexed
    
    async def search_docs(self, query: str, site: str = None) -> List[Dict]:
        """Search indexed documentation"""
        if not self.cached_pages:
            return []
        
        query_lower = query.lower()
        results = []
        
        for url, page in self.cached_pages.items():
            # Filter by site if specified
            if site and page.site != site:
                continue
            
            # Calculate relevance score
            score = 0
            title_lower = page.title.lower()
            content_lower = page.content.lower()
            
            # Higher score for title matches
            if query_lower in title_lower:
                score += 10
                # Even higher for exact title matches
                if query_lower == title_lower:
                    score += 20
            
            # Score for content matches
            content_matches = content_lower.count(query_lower)
            score += content_matches * 2
            
            # Score for partial word matches in title
            query_words = query_lower.split()
            for word in query_words:
                if word in title_lower:
                    score += 5
                if word in content_lower:
                    score += 1
            
            if score > 0:
                # Extract snippet around first match
                snippet = self._extract_snippet(page.content, query, max_length=200)
                
                results.append({
                    'title': page.title,
                    'url': page.url,
                    'site': page.site,
                    'snippet': snippet,
                    'score': score
                })
        
        # Sort by relevance score (highest first) and limit results
        results.sort(key=lambda x: x['score'], reverse=True)
        return results[:10]
    
    def _extract_snippet(self, content: str, query: str, max_length: int = 200) -> str:
        """Extract a snippet of content around the query match"""
        query_lower = query.lower()
        content_lower = content.lower()
        
        # Find the first occurrence of the query
        match_index = content_lower.find(query_lower)
        
        if match_index == -1:
            # If no exact match, return beginning of content
            return content[:max_length] + "..." if len(content) > max_length else content
        
        # Calculate snippet boundaries
        start = max(0, match_index - max_length // 2)
        end = min(len(content), start + max_length)
        
        # Adjust start if we're near the end
        if end - start < max_length:
            start = max(0, end - max_length)
        
        snippet = content[start:end]
        
        # Add ellipsis if we're not at the beginning/end
        if start > 0:
            snippet = "..." + snippet
        if end < len(content):
            snippet = snippet + "..."
        
        return snippet

# Initialize the MCP server
mcp = FastMCP("Cortex Cloud Docs MCP Server")

# Initialize indexer
indexer = DocumentationIndexer()

@mcp.tool()
async def search_cortex_docs(query: str) -> str:
    """Search Cortex Cloud documentation"""
    results = await indexer.search_docs(query, site='cortex_cloud')
    return json.dumps(results, indent=2)

@mcp.tool()
async def search_cortex_api_docs(query: str) -> str:
    """Search Cortex Cloud API documentation"""
    results = await indexer.search_docs(query, site='cortex_api')
    return json.dumps(results, indent=2)

@mcp.tool()
async def search_all_docs(query: str) -> str:
    """Search across all Cortex Cloud documentation sites."""
    results = await indexer.search_docs(query)
    return json.dumps(results, indent=2)

@mcp.tool()
async def index_cortex_docs(max_pages: int = 50) -> str:
    """Index Cortex Cloud documentation. Call this first before searching."""
    pages_indexed = await indexer.index_site('cortex_cloud', max_pages)
    return f"Indexed {pages_indexed} pages from Cortex Cloud documentation"

@mcp.tool()
async def index_cortex_api_docs(max_pages: int = 50) -> str:
    """Index Cortex Cloud API documentation. Call this first before searching."""
    pages_indexed = await indexer.index_site('cortex_api', max_pages)
    return f"Indexed {pages_indexed} pages from Cortex Cloud API documentation"

@mcp.tool()
async def get_index_status() -> str:
    """Check how many documents are currently cached."""
    total_docs = len(indexer.cached_pages)
    sites = {}
    for page in indexer.cached_pages.values():
        site = page.site
        sites[site] = sites.get(site, 0) + 1
    
    # Also show cache statistics
    expired_count = sum(1 for page in indexer.cached_pages.values() if page.is_expired)
    
    return json.dumps({
        'total_cached_pages': total_docs,
        'expired_pages': expired_count,
        'search_cache_entries': len(indexer.search_cache),
        'by_site': sites
    }, indent=2)

def main():
    """Main entry point for HTTP mode deployment"""
    print("MCP Server starting in HTTP mode...")
    
    # Use Smithery-required PORT environment variable
    port = int(os.environ.get("PORT", 8081))
    print(f"Listening on port {port}")

    # Set environment variable for port and run
    os.environ["PORT"] = str(port)
    mcp.run()

if __name__ == "__main__":
    main()