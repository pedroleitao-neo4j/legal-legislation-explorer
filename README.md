
# UK Legislation Graph Explorer

> **Work in progress:** This tool is not yet fully reliable for extracting the entire UK legislation corpus, but it is a starting point for building a graph database of UK legislative documents. It is designed to be extensible and adaptable to the complex XML format used by [legislation.gov.uk](https://www.legislation.gov.uk/).

This repository provides a [crawler](crawler.ipynb) and [data loader](loader.ipynb) for structured legislation documents following the [CLML Schema](https://github.com/legislation/clml-schema). It helps you build a graph database of official legislation **without** needing a complex ETL pipeline, PDF processing, or manual data cleaning. The crawler extracts structured data directly from the XML files, and the loader transforms and loads this data into Neo4j.

The schema for UK legislation is complex - the crawler and loader handle much of this complexity by extracting the hierarchy of legislation (Parts, Chapters, Sections, Paragraphs, Schedules, Subparagraphs, Explanatory Notes, etc.), as well as citations, cross-references, commentaries, and related information - and turning it into a ready made graph database. It aims to capture as much structure as possible, supporting complex and temporal queries.

> The [loader](loader.ipynb) uses [pyspark](https://spark.apache.org/docs/latest/api/python/index.html) to transform raw JSON data for Neo4j. You can refactor it to use plain Python if needed.

## The Graph Schema

The resulting graph schema is designed to capture the hierarchical structure of legislation, as well as relationships between different pieces of legislation, citations, and commentaries.

![Graph Schema](renderings/graph_schema.png)

## Legislation Parser

The [crawler](crawler.ipynb) extracts the hierarchy of legislation from a [seed list](legislation_list.txt), including:

- **`:Legislation`**: The root node for each piece of legislation, with properties like `uri`, `title`, `type`, `enacted_date`, etc.
- **:`:Part`, `:Chapter`, `:Section`, `:Paragraph`**: Nodes representing the structural hierarchy of legislation, with properties like `number`, `title`, and `text`.
- **`:Schedule`, `:ScheduleParagraph`, `:ScheduleSubparagraph`**: Nodes representing schedules and their components.
- **`:ExplanatoryNotes`**: Explanatory notes included in legislation
- **`:Citation`**: Nodes representing citations to other legal acts and provisions
- **`:Commentary`**: Nodes representing commentaries linked to specific paragraphs or sections.

## Example Cypher Queries

The [`examples.ipynb`](examples.ipynb) notebook demonstrates how to run Cypher queries against the graph database to explore relationships between legislation, retrieve text, etc.

## TODO

- ~~**Unique IDs:**~~
  - ~~IDs like commentary and ref IDs are only unique within a single legislation. Unique IDs must be generated when loading into the graph database.~~

- **Citations, Sub-Refs, and Commentary:**
  - The structure is currently messy and redundant. Needs refactoring. Commentaries should be linked to their respective paragraph (`CommentaryRef`).

- **Ordering:**
  - Add remaining `order` properties to all nodes.