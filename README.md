
# UK Legislation Graph Explorer

> **Work in progress:** This tool is not yet fully reliable for extracting the entire UK legislation corpus, but it is a starting point for building a graph database of UK legislative documents. It is designed to be extensible and adaptable to the complex XML format used by [legislation.gov.uk](https://www.legislation.gov.uk/).

This repository provides a [crawler](crawler.ipynb) and [data loader](loader.ipynb) for UK legislation. It helps you build a graph database of UK legal documents **without** needing a complex ETL pipeline, PDF processing, or manual data cleaning. The crawler extracts structured data directly from the XML files, and the loader transforms this data into a format ready for Neo4j.

The XML format for UK legislation is complex. The crawler and loader handle much of this complexity by extracting the hierarchy of legislation (Parts, Chapters, Sections, Paragraphs, Schedules, Subparagraphs, Explanatory Notes, etc.), as well as citations, cross-references, commentaries, and related information.

This project aims to capture as much structure as possible, supporting complex queries and multiple versions of legislation. However, using it may require some understanding of the legislative corpus.

> The [loader](loader.ipynb) uses [pyspark](https://spark.apache.org/docs/latest/api/python/index.html) to transform raw JSON data for Neo4j. You can refactor it to use plain Python if needed.

## Legislation Parser

The [crawler](crawler.ipynb) extracts the hierarchy of legislation from a [seed list](legislation_list.txt), including:

- **Legislation**: Acts, Statutory Instruments, or Draft Statutory Instruments
- **Metadata**: Title, year, and type
- **Superstructure**: Hierarchy (supersedes, superseded-by)
- **Parts**: Main divisions
- **Chapters**: Divisions of a Part
- **Sections**: Divisions of a Chapter
- **Paragraphs**: Divisions of a Section
- **Schedules**: Additional divisions
- **Subparagraphs**: Divisions of a Schedule
- **Commentaries**: Notes on the legislation
- **Citations**: References to other legislation
- **Sub-Refs**: Sub-references to other legislation
- **Explanatory Notes**: Additional explanations

## Example Cypher Queries

All legislation directly or indirectly connected to a piece of legislation:
```cypher
// Anchor the query on the target legislation
MATCH (target:Legislation)
WHERE target.uri CONTAINS "uksi/2020/1495"
// Traverse backwards from the target, up through the citation nodes and the structural hierarchy to the source Act
MATCH p = (source:Legislation)-[:HAS_PART|HAS_CHAPTER|HAS_SECTION|HAS_PARAGRAPH|HAS_SCHEDULE|HAS_SUBPARAGRAPH|HAS_COMMENTARY|HAS_CITATION|HAS_SUBREF*1..5]->(citation_link)-[:CITES_ACT|REFERENCES]->(target)
// Exclude self-citations (where the source Act is the same as the target Act)
WHERE source.uri <> target.uri
RETURN p
```

A piece of legislation down to its sections and paragraphs:
```cypher
MATCH p=(l:Legislation)-[:HAS_PART]->(:Part)-[:HAS_CHAPTER]->(:Chapter)-[:HAS_SECTION]->(:Section)-[:HAS_PARAGRAPH]->(:Paragraph)
WHERE l.uri CONTAINS "ukpga/2010/4"
RETURN p
```

All commentaries which cite a piece of legislation:
```cypher
MATCH p=(:Commentary)-[:HAS_CITATION]->(:Citation)-[:CITES_ACT]->(l:Legislation)
WHERE l.uri CONTAINS "uksi/2020/1495"
RETURN p
```

All paragraphs with commentary that cite a piece of legislation:
```cypher
MATCH p=(:Paragraph)-[:HAS_COMMENTARY]->(:Commentary)-[:HAS_CITATION]->(:Citation)-[:CITES_ACT]->(l:Legislation)
WHERE l.uri CONTAINS "uksi/2020/1495"
RETURN p
```

All Schedules and their paragraphs, with any commentary that cites a piece of legislation:
```cypher
MATCH p=(l:Legislation)-[:HAS_SCHEDULE]->(sc:Schedule)-[:HAS_PARAGRAPH]->(scp:ScheduleParagraph)-[:HAS_SUBPARAGRAPH]->(scsp:ScheduleSubparagraph)
WHERE l.uri CONTAINS "ukpga/2010/4"
OPTIONAL MATCH (scp)-[:HAS_COMMENTARY]-(:Commentary)-[:HAS_CITATION]-(:Citation)-[:HAS_SUBREF]->(:CitationSubRef)
RETURN p
```

Create synthetic relationships between Acts that cite each other, and store the count of citations as a 'weight' property:
> This will create **many** relationships, so use with care.
```cypher
// Find all deep paths where one Act cites another
MATCH (source:Legislation)-[:HAS_PART|HAS_CHAPTER|HAS_SECTION|HAS_PARAGRAPH|HAS_SCHEDULE|HAS_SUBPARAGRAPH|HAS_COMMENTARY|HAS_CITATION|HAS_SUBREF*1..10]->(citation_link)-[:CITES_ACT|REFERENCES]->(target:Legislation)

// Prevent self-citations from looping on the same node
WHERE source.uri <> target.uri

// Aggregate the results to count how many individual citations exist between them
WITH source, target, count(citation_link) AS citation_count

// Create the new synthetic relationship and store the count as a 'weight'
MERGE (source)-[rel:CITES_LEGISLATION]->(target)
SET rel.weight = citation_count
```

Match interconnected legislation based on the synthetic CITES_LEGISLATION relationships:
```cypher
MATCH p = (l1:Legislation)-[r:CITES_LEGISLATION]->(l2:Legislation)
RETURN p
LIMIT 1000
```

Compute the top 10 most cited pieces of legislation:
```cypher
// Match the synthetic relationships between Acts
MATCH (source:Legislation)-[r:CITES_LEGISLATION]->(target:Legislation)
// Aggregate the data per target legislation
RETURN target.uri AS CitedLegislation, 
       count(source) AS IncomingLegislationCount, 
       sum(r.weight) AS TotalCitations
// Order by the highest number of total citations and limit to the top 10
ORDER BY TotalCitations DESC
LIMIT 10
```

All explanatory notes which cite a piece of legislation:
```cypher
MATCH p=(:ExplanatoryNotes)-[:HAS_PARAGRAPH]->(:ExplanatoryNotesParagraph)-[:HAS_CITATION]-(:Citation)-[:CITES_ACT]->(l:Legislation)
WHERE l.uri CONTAINS "uksi/2020/1495"
RETURN p
```

All explanatory notes and their citations for a given piece of legislation:
```cypher
MATCH p=(l:Legislation)-[:HAS_EXPLANATORY_NOTES]->(:ExplanatoryNotes)-[:HAS_PARAGRAPH]->(enp:ExplanatoryNotesParagraph)
WHERE l.uri CONTAINS "uksi/2024/1012"
OPTIONAL MATCH cp=(enp)-[:HAS_CITATION]-(:Citation)
RETURN p,cp
```

The network of superseded legislation:
```cypher
MATCH p=(:Legislation)-[:SUPERSEDED_BY|SUPERSEDES]-(:Legislation)
RETURN p
```

Retrieve all items of a given piece of legislation, in order:
```cypher
MATCH p=(l:Legislation)-[*1..]->(n)
WHERE l.uri CONTAINS "ukpga/2020/14"
  AND all(x IN nodes(p) WHERE x:Legislation OR x:Part OR x:Chapter OR x:Section OR x:Paragraph)
RETURN 
  n.uri AS uri, 
  labels(n)[0] AS type, 
  n.order AS order, 
  coalesce(n.text, n.title) AS text
ORDER BY [x IN nodes(p) WHERE x.order IS NOT NULL | x.order]
```

Rebuild the full text for a given legislation from the graph:
```cypher
MATCH p=(l:Legislation)-[*1..]->(n)
WHERE l.uri CONTAINS "ukpga/2020/14"
  AND all(x IN nodes(p) WHERE x:Legislation OR x:Part OR x:Chapter OR x:Section OR x:Paragraph)
  AND coalesce(n.text, n.title) IS NOT NULL // Combined with AND
WITH n, p
ORDER BY [x IN nodes(p) WHERE x.order IS NOT NULL | x.order]
WITH collect(coalesce(n.text, n.title)) AS text_parts
RETURN reduce(document = "", part IN text_parts | document + part + "\n\n") AS full_text
```

## TODO

- ~~**Unique IDs:**~~
  - ~~IDs like commentary and ref IDs are only unique within a single legislation. Unique IDs must be generated when loading into the graph database.~~

- **Citations, Sub-Refs, and Commentary:**
  - The structure is currently messy and redundant. Needs refactoring. Commentaries should be linked to their respective paragraph (`CommentaryRef`).

- **Ordering:**
  - Add remaining `order` properties to all nodes.