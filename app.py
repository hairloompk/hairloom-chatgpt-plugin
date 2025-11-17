# app.py
import os
import json
import uvicorn
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, JSONResponse

# Load env
SHOP = os.getenv("SHOP_DOMAIN")  # e.g. "hairloom.myshopify.com" or "hairloom.pk"
TOKEN = os.getenv("SHOPIFY_STOREFRONT_TOKEN")  # Storefront access token
API_BASE = f"https://{SHOP}/api/2024-10/graphql.json"  # update API version if needed

# Paths to static manifest files in repo (we'll return them)
AI_PLUGIN_PATH = os.path.join(os.path.dirname(__file__), "ai-plugin.json")
OPENAPI_PATH = os.path.join(os.path.dirname(__file__), "openapi.yaml")

app = FastAPI(title="Hairloom Plugin Proxy")

# Restrict origins in prod; ChatGPT origin allowed for discovery/invocation
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://chat.openai.com"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

HEADERS = {
    "X-Shopify-Storefront-Access-Token": TOKEN,
    "Content-Type": "application/json"
}

def shopify_graphql(query: str, variables: dict = None):
    if not SHOP or not TOKEN:
        raise HTTPException(status_code=500, detail="SHOP_DOMAIN or SHOPIFY_STOREFRONT_TOKEN not set")
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(API_BASE, json=payload, headers=HEADERS, timeout=10)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Shopify API error: {resp.status_code} {resp.text}")
    data = resp.json()
    if "errors" in data:
        raise HTTPException(status_code=502, detail=data["errors"])
    return data.get("data")

# Serve the plugin manifest and OpenAPI files so ChatGPT can discover the plugin.
@app.get("/.well-known/ai-plugin.json", response_class=JSONResponse)
async def plugin_manifest():
    return JSONResponse(content=json.load(open(AI_PLUGIN_PATH, "r", encoding="utf-8")))

@app.get("/openapi.yaml")
async def openapi_yaml():
    return FileResponse(OPENAPI_PATH, media_type="text/yaml")

# --- API endpoints used by the plugin ---
@app.get("/search")
async def search(q: str, limit: int = 5):
    # GraphQL query to search products & articles
    product_query = """
    query($query:String!, $limit:Int!) {
      products(query: $query, first: $limit) {
        edges {
          node {
            id
            title
            handle
            description
            onlineStoreUrl
            images(first:1) { edges { node { url } } }
            priceRange { minVariantPrice { amount currencyCode } }
          }
        }
      }
      articles(query: $query, first: $limit) {
        edges {
          node {
            id
            title
            handle
            excerpt
            onlineStoreUrl
          }
        }
      }
    }
    """
    variables = {"query": q, "limit": limit}
    data = shopify_graphql(product_query, variables)
    results = []
    products = data.get("products", {}).get("edges", [])
    for p in products:
        node = p["node"]
        url = node.get("onlineStoreUrl") or f"https://{SHOP}/products/{node.get('handle')}"
        image_edges = node.get("images", {}).get("edges", [])
        image = image_edges[0]["node"]["url"] if image_edges else None
        price = node.get("priceRange", {}).get("minVariantPrice", {}).get("amount")
        results.append({
            "id": node.get("id"),
            "title": node.get("title"),
            "url": url,
            "snippet": (node.get("description") or "")[:300],
            "score": 1.0,
            "image": image,
            "price": price
        })
    articles = data.get("articles", {}).get("edges", [])
    for a in articles:
        node = a["node"]
        url = node.get("onlineStoreUrl") or f"https://{SHOP}/blogs/news/{node.get('handle')}"
        results.append({
            "id": node.get("id"),
            "title": node.get("title"),
            "url": url,
            "snippet": (node.get("excerpt") or "")[:300],
            "score": 0.8
        })
    return {"query": q, "results": results[:limit]}

@app.get("/product/{id_or_handle}")
async def product(id_or_handle: str):
    query = """
    query($handle:String!) {
      productByHandle(handle:$handle) {
        id
        title
        handle
        description
        onlineStoreUrl
        images(first:10) { edges { node { url } } }
        priceRange { minVariantPrice { amount currencyCode } }
        variants(first:5) { edges { node { id title price } } }
      }
    }
    """
    variables = {"handle": id_or_handle}
    data = shopify_graphql(query, variables)
    prod = data.get("productByHandle")
    if not prod:
        raise HTTPException(status_code=404, detail="Product not found by handle")
    images = [e["node"]["url"] for e in prod.get("images", {}).get("edges", [])]
    price = prod.get("priceRange", {}).get("minVariantPrice", {}).get("amount")
    return {
        "id": prod.get("id"),
        "title": prod.get("title"),
        "description": prod.get("description"),
        "url": prod.get("onlineStoreUrl") or f"https://{SHOP}/products/{prod.get('handle')}",
        "price": price,
        "images": images
    }

@app.get("/blog/{slug}")
async def blog(slug: str):
    query = """
    query($handle:String!) {
      articleByHandle(handle:$handle) {
        id
        title
        content
        excerpt
        onlineStoreUrl
      }
    }
    """
    variables = {"handle": slug}
    data = shopify_graphql(query, variables)
    art = data.get("articleByHandle")
    if not art:
        raise HTTPException(status_code=404, detail="Article not found")
    return {
        "title": art.get("title"),
        "url": art.get("onlineStoreUrl") or f"https://{SHOP}/blogs/news/{slug}",
        "excerpt": art.get("excerpt"),
        "content": art.get("content")
    }

@app.get("/faq")
async def faq():
    query = """
    query {
      pages(first:10, query:"title:FAQ") {
        edges {
          node {
            id
            title
            body
            handle
            onlineStoreUrl
          }
        }
      }
    }
    """
    data = shopify_graphql(query)
    pages = data.get("pages", {}).get("edges", [])
    faqs = []
    for p in pages:
        node = p["node"]
        faqs.append({
            "question": node.get("title"),
            "answer": node.get("body")
        })
    return {"faqs": faqs}

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
