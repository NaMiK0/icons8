"""Сборка HTML-файлов из кластеров.

«Связанные лендинги» понимаются в двух смыслах сразу: страницы связаны с
кластерами запросов и связаны между собой. Отсюда хаб index.html и блок
Related pages с keyword-анкорами на каждой странице — набор, а не десять
одиночных файлов.
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from ..domain.models import Cluster
from .inline import asset_svg

TEMPLATES = Path(__file__).parent / "templates"

ASSETS_PER_PAGE = 8
SEARCHES_SHOWN = 12


@dataclass(frozen=True, slots=True)
class SiteMeta:
    """Общие для всех страниц сведения о прогоне."""

    site_name: str
    base_url: str
    source_file: str
    content_source: str
    cluster_source: str
    language: str = "en"
    generated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "site_name": self.site_name,
            "source_file": self.source_file,
            "content_source": self.content_source,
            "cluster_source": self.cluster_source,
            "generated_at": self.generated_at or date.today().isoformat(),
        }


class Renderer:
    def __init__(self, meta: SiteMeta):
        self.meta = meta
        self.env = Environment(
            loader=FileSystemLoader(TEMPLATES),
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render_site(
        self, clusters: list[Cluster], out_dir: Path, excluded: list[dict[str, Any]] | None = None
    ) -> list[Path]:
        """Записать хаб и по странице на кластер.

        Ранее сгенерированные страницы удаляются: набор кластеров меняется
        от прогона к прогону, и без очистки в выдаче копятся файлы от
        прошлых запусков — со ссылками, которые уже никуда не ведут.
        """
        out_dir.mkdir(parents=True, exist_ok=True)
        for stale in out_dir.glob("*.html"):
            stale.unlink()

        written = [self._render_index(clusters, out_dir, excluded or [])]
        for cluster in clusters:
            written.append(self._render_landing(cluster, clusters, out_dir))
        return written

    def _render_landing(self, cluster: Cluster, all_clusters: list[Cluster], out_dir: Path) -> Path:
        content = cluster.content
        if content is None:
            raise ValueError(f"у кластера {cluster.slug} нет наполнения")

        related = [
            {"href": f"{other.slug}.html", "anchor": _anchor(other)}
            for other in all_clusters
            if other.slug != cluster.slug
        ]
        queries = cluster.queries
        assets = [
            {"svg": Markup(asset_svg(query.text, index)), "label": query.text}
            for index, query in enumerate(queries[:ASSETS_PER_PAGE])
        ]
        searches = [
            {
                "text": query.text,
                "clicks": f"{query.clicks:,}".replace(",", " "),
                "impressions": f"{query.impressions:,}".replace(",", " "),
                "position": "—" if query.position is None else f"{query.position:.1f}",
            }
            for query in queries[:SEARCHES_SHOWN]
        ]

        html = self.env.get_template("landing.html").render(
            lang=self.meta.language,
            page_title=content.title,
            meta_description=content.meta_description,
            canonical=self._url(f"{cluster.slug}.html"),
            breadcrumbs=[
                {"name": self.meta.site_name, "url": self.meta.base_url},
                {"name": "Landing pages", "url": "index.html"},
                {"name": content.h1, "url": f"{cluster.slug}.html"},
            ],
            content=content,
            assets=assets,
            searches=searches,
            related=related,
            meta=self.meta.as_dict(),
            json_ld=self._landing_json_ld(cluster, content),
        )
        return _write(out_dir / f"{cluster.slug}.html", html)

    def _render_index(
        self, clusters: list[Cluster], out_dir: Path, excluded: list[dict[str, Any]]
    ) -> Path:
        pages = [
            {
                "href": f"{cluster.slug}.html",
                "anchor": _anchor(cluster),
                "queries": len(cluster.queries),
                "clicks": f"{cluster.clicks:,}".replace(",", " "),
                "impressions": f"{cluster.impressions:,}".replace(",", " "),
                "position": "—" if cluster.avg_position is None else f"{cluster.avg_position:.1f}",
                "potential": f"{cluster.potential:,.0f}".replace(",", " "),
            }
            for cluster in clusters
        ]
        title = f"Landing pages for {self.meta.site_name}"
        html = self.env.get_template("index.html").render(
            lang=self.meta.language,
            page_title=title,
            meta_description=(
                f"{len(clusters)} landing pages built from a Search Console export "
                f"for {self.meta.site_name}."
            ),
            canonical=self._url("index.html"),
            breadcrumbs=[
                {"name": self.meta.site_name, "url": self.meta.base_url},
                {"name": "Landing pages", "url": "index.html"},
            ],
            heading=title,
            intro=(
                f"{len(clusters)} pages, each built from one cluster of related search "
                f"queries in {self.meta.source_file}. Every page links to the others."
            ),
            pages=pages,
            excluded=excluded,
            meta=self.meta.as_dict(),
            json_ld=self._index_json_ld(clusters),
        )
        return _write(out_dir / "index.html", html)

    def _url(self, path: str) -> str:
        return self.meta.base_url.rstrip("/") + "/" + path.lstrip("/")

    def _landing_json_ld(self, cluster: Cluster, content) -> dict[str, Any]:
        graph: list[dict[str, Any]] = [
            {
                "@type": "WebPage",
                "name": content.title,
                "description": content.meta_description,
                "url": self._url(f"{cluster.slug}.html"),
                "inLanguage": self.meta.language,
            },
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": self.meta.site_name,
                     "item": self.meta.base_url},
                    {"@type": "ListItem", "position": 2, "name": "Landing pages",
                     "item": self._url("index.html")},
                    {"@type": "ListItem", "position": 3, "name": content.h1,
                     "item": self._url(f"{cluster.slug}.html")},
                ],
            },
        ]
        if content.faq:
            graph.append(
                {
                    "@type": "FAQPage",
                    "mainEntity": [
                        {
                            "@type": "Question",
                            "name": item.question,
                            "acceptedAnswer": {"@type": "Answer", "text": item.answer},
                        }
                        for item in content.faq
                    ],
                }
            )
        return {"@context": "https://schema.org", "@graph": graph}

    def _index_json_ld(self, clusters: list[Cluster]) -> dict[str, Any]:
        return {
            "@context": "https://schema.org",
            "@type": "CollectionPage",
            "name": f"Landing pages for {self.meta.site_name}",
            "url": self._url("index.html"),
            "inLanguage": self.meta.language,
            "hasPart": [
                {
                    "@type": "WebPage",
                    "name": cluster.content.title if cluster.content else cluster.primary_keyword,
                    "url": self._url(f"{cluster.slug}.html"),
                }
                for cluster in clusters
            ],
        }


def _anchor(cluster: Cluster) -> str:
    content = cluster.content
    if content and content.anchor_text:
        return content.anchor_text
    return cluster.primary_keyword


def _write(path: Path, html: str) -> Path:
    path.write_text(html.rstrip() + "\n", encoding="utf-8")
    return path
