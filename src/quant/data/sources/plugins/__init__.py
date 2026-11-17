"""One module per data source. Each module defines a DataSource subclass decorated with `@register`; `quant.data.sources.registry.discover` imports
every module here, so a new source is one new file (the wiki page Adding-a-dataset). A module's licence row in quant.core.licences is the row its dataset names."""
