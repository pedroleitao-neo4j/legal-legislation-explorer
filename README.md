# A UK Legislation Graph Explorer

> ***Work in progress***, this is not yet reliable enough for 100% fidelity extraction of the entire UK legislation corpus, but it is a starting point for building a graph database of legislative documents, and it is designed to be easily extensible and adaptable to handle the complexities of the underlying XML format.

This repository contains a [crawler](crawler.ipynb) and [data loader](data_loader.ipynb) for [UK Legislation](https://www.legislation.gov.uk/). It is intended to be used as a starting point for building a graph database of the legal corpus of the United Kingdom, **without** the need for a complex ETL pipeline, PDF processing, manual data cleaning, or other time-consuming and error-prone processes when trying to build a graph representation of unstructured documents. The crawler directly extracts structured data from the XML files provided by [legislation.gov.uk](https://www.legislation.gov.uk/), and the data loader transforms this structured data into a graph format suitable for loading into Neo4j.

The XML format for UK legislation is known to be fiendishly complex, and the crawler and data loader are designed to handle (partially) this complexity. The crawler extracts the (almost) full hierarchy of legislation, including Parts, Chapters, Sections, Paragraphs, Schedules, Subparagraphs, Explanatory Notes, and Explanatory Notes Paragraphs. It also extracts citations and cross-references between different pieces of legislation, as well as commentaries and other related information.

It is designed to capture as much structure as possible, meaning it can accommodate complex structural and time-varying queries, as well as the ability to handle multiple versions of the same piece of legislation. However because it attempts to capture as much of the semantical structure as possible, it also requires a reasonable understanding of the legislative corpus (i.e., it is not ***simple***).

> The [loader](loader.ipynb) currently uses [pyspark](https://spark.apache.org/docs/latest/api/python/index.html) to transform the raw JSON data into a format suitable for loading into Neo4j. This is because the transformation process involves some data manipulation, and pyspark provides a powerful and flexible architecture to handle this. However, it is possible to refactor the loader to use plain Python if so desired.

### The legislation parser

The legislation parser is a [crawler](crawler.ipynb) which extracts the (almost) full hierarchy of legislation from a [seed list of URI's](legislation_list.txt) (up to a configurable depth of linked legislation), including Parts, Chapters, Sections, Paragraphs, Schedules, and Subparagraphs. It also extracts citations and cross-references between different pieces of legislation, as well as commentaries and other related information.

- **Legislation**: The main piece of legislation, which can be an Act, a Statutory Instrument, or a Draft Statutory Instrument.
- **Metadata**: Metadata about the legislation, such as the title, year, and type.
- **Superstructure**: The superstructure of the legislation, which is the hierarchy of the legislation (supersedes, superseded-by)
- **Parts**: The main divisions of a piece of legislation.
- **Chapters**: The divisions of a Part.
- **Sections**: The divisions of a Chapter.
- **Paragraphs**: The divisions of a Section.
- **Schedules**: The divisions of a piece of legislation which are not Parts, Chapters, Sections, or Paragraphs.
- **Subparagraphs**: The divisions of a Schedule.
- **Commentaries**: Commentaries on the legislation.
- **Citations**: Citations to other pieces of legislation.
- **Sub-Refs**: Sub-references to other pieces of legislation.
- **Explanatory Notes**: Explanatory notes on the legislation.

### Example Queries

All legislation which directly or indirectly connects to a piece of legislation.
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

A piece of legislation down to its sections and paragraphs.
```cypher
MATCH p=(l:Legislation)-[:HAS_PART]->(:Part)-[:HAS_CHAPTER]->(:Chapter)-[:HAS_SECTION]->(:Section)-[:HAS_PARAGRAPH]->(:Paragraph)
WHERE l.uri CONTAINS "ukpga/2010/4"
RETURN p
```

All commentaries which cite a piece of legislation.
```cypher
MATCH p=(:Commentary)-[:HAS_CITATION]->(:Citation)-[:CITES_ACT]->(l:Legislation)
WHERE l.uri CONTAINS "uksi/2020/1495"
RETURN p
```

All paragraphs which have commentary which cite a piece of legislation.
```cypher
MATCH p=(:Paragraph)-[:HAS_COMMENTARY]->(:Commentary)-[:HAS_CITATION]->(:Citation)-[:CITES_ACT]->(l:Legislation)
WHERE l.uri CONTAINS "uksi/2020/1495"
RETURN p
```

All Schedules and their paragraphs, together with any commentary which cite a piece of legislation.
```cypher
MATCH p=(l:Legislation)-[:HAS_SCHEDULE]->(sc:Schedule)-[:HAS_PARAGRAPH]->(scp:ScheduleParagraph)-[:HAS_SUBPARAGRAPH]->(scsp:ScheduleSubparagraph)
WHERE l.uri CONTAINS "ukpga/2010/4"
OPTIONAL MATCH (scp)-[:HAS_COMMENTARY]-(:Commentary)-[:HAS_CITATION]-(:Citation)-[:HAS_SUBREF]->(:CitationSubRef)
RETURN p
```

Create synthetic relationships between Acts which cite each other, and store the count of individual citations as a 'weight' property on the relationship.
> This will create **a lot** of relationships, so use with care.
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

Match interconnected legislation based on the synthetic CITES_LEGISLATION relationships, which represent the overall citation network between different pieces of legislation.
```cypher
MATCH p = (l1:Legislation)-[r:CITES_LEGISLATION]->(l2:Legislation)
RETURN p
LIMIT 1000
```

Compute the top 10 most cited pieces of legislation.
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

All explanatory notes which cite a piece of legislation.
```cypher
MATCH p=(:ExplanatoryNotes)-[:HAS_PARAGRAPH]->(:ExplanatoryNotesParagraph)-[:HAS_CITATION]-(:Citation)-[:CITES_ACT]->(l:Legislation)
WHERE l.uri CONTAINS "uksi/2020/1495"
RETURN p
```

All explanatory notes, and their citations, included with a given piece of legislation.
```cypher
MATCH p=(l:Legislation)-[:HAS_EXPLANATORY_NOTES]->(:ExplanatoryNotes)-[:HAS_PARAGRAPH]->(enp:ExplanatoryNotesParagraph)
WHERE l.uri CONTAINS "uksi/2024/1012"
OPTIONAL MATCH cp=(enp)-[:HAS_CITATION]-(:Citation)
RETURN p,cp
```

The network of superseded legislation.
```cypher
MATCH p=(:Legislation)-[:SUPERSEDED_BY|SUPERSEDES]-(:Legislation)
RETURN p
```

Retrieve all the various items of a given piece of legislation, in the order they appear.
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

Rebuild the whole text for a given legislation, from the graph.
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

### Unique IDs

~~ID's such as commentary, and ref IDs are not unique across the entire dataset. They are only unique within the context of a single legislation. This means that we need to generate unique IDs for these entities when we load them into the graph database.~~

### Citations, Sub-Refs, and Commentary

It is currently a bit messy and redundant, needs to be refactored. Commentaries also need to be linked to their respective paragraph (`CommentaryRef`).

### Ordering

Add remaining `order` properties to all nodes.