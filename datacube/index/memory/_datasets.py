# This file is part of the Open Data Cube, see https://opendatacube.org for more information
#
# Copyright (c) 2015-2020 ODC Contributors
# SPDX-License-Identifier: Apache-2.0
import datetime
import logging
import warnings
from collections import namedtuple
from typing import Callable, Iterable, List, Mapping, Optional, Set, Tuple, Union
from uuid import UUID

from datacube.index import fields

from datacube.index.abstract import AbstractDatasetResource, DSID, dsid_to_uuid, QueryField
from datacube.index.fields import Field
from datacube.index.memory._products import ProductResource
from datacube.model import Dataset, DatasetType as Product
from datacube.utils import jsonify_document, _readable_offset
from datacube.utils import changes
from datacube.utils.changes import AllowPolicy, Change, Offset, get_doc_changes
from datacube.utils.documents import metadata_subset

_LOG = logging.getLogger(__name__)


class DatasetResource(AbstractDatasetResource):
    def __init__(self, product_resource: ProductResource) -> None:
        self.product_resource = product_resource
        self.metadata_type_resource = product_resource.metadata_type_resource
        # Main dataset index
        self.by_id: Mapping[UUID, Dataset] = {}
        # Indexes for active and archived datasets
        self.active_by_id: Mapping[UUID, Dataset] = {}
        self.archived_by_id: Mapping[UUID, Dataset] = {}
        # Lineage indexes:
        self.derived_from: Mapping[UUID, Mapping[str, UUID]] = {}
        self.derivations: Mapping[UUID, Mapping[str, UUID]] = {}
        # Location registers
        self.locations: Mapping[UUID, List[str]] = {}
        self.archived_locations: Mapping[UUID, List[Tuple[str, datetime.datetime]]] = {}
        # Active Index By Product
        self.by_product: Mapping[str, List[UUID]] = {}

    def get(self, id_: DSID, include_sources: bool = False) -> Optional[Dataset]:
        try:
            ds = self.clone(self.by_id[dsid_to_uuid(id_)])
            if include_sources:
                ds.sources = {
                    classifier: self.get(dsid, include_sources=True)
                    for classifier, dsid in self.derived_from.get(ds.id, {}).items()
                }
            return ds
        except KeyError:
            return None

    def bulk_get(self, ids: Iterable[DSID]) -> Iterable[Dataset]:
        return (ds for ds in (self.get(dsid) for dsid in ids) if ds is not None)

    def get_derived(self, id_: DSID) -> Iterable[Dataset]:
        return (self.get(dsid) for dsid in self.derivations.get(dsid_to_uuid(id_), {}).values())

    def has(self, id_: DSID) -> bool:
        return dsid_to_uuid(id_) in self.by_id

    def bulk_has(self, ids_: Iterable[DSID]) -> Iterable[bool]:
        return (self.has(id_) for id_ in ids_)

    def add(self, dataset: Dataset,
            with_lineage: Optional[bool] = None,
            **kwargs) -> Dataset:
        if with_lineage is None:
            with_lineage = True
        _LOG.info('indexing %s', dataset.id)
        if with_lineage:
            # Add base dataset without lineage
            self.add(dataset, with_lineage=False)
            # Add lineage
            for classifier, src in dataset.sources.items():
                # Recursively add source dataset and lineage
                self.add(src, with_lineage=True)
                self.persist_source_relationship(dataset, src, classifier)
        else:
            if self.has(dataset.id):
                _LOG.warning("Dataset %s is already in the database", dataset.id)
                return dataset
            persistable = self.clone(dataset, for_save=True)
            self.by_id[persistable.id] = persistable
            self.active_by_id[persistable.id] = persistable
            if dataset.uris is not None:
                self.locations[persistable.id] = dataset.uris.copy()
            else:
                self.locations[persistable.id] = []
            self.archived_locations[persistable.id] = []
            if dataset.type.name in self.by_product:
                self.by_product[dataset.type.name].append(dataset.id)
            else:
                self.by_product[dataset.type.name] = [dataset.id]
        return self.get(dataset.id)

    def persist_source_relationship(self, ds: Dataset, src: Dataset, classifier: str) -> None:
        # Add source lineage link
        if ds.id not in self.derived_from:
            self.derived_from[ds.id] = {}
        if self.derived_from[ds.id].get(classifier, src.id) != src.id:
            _LOG.warning("Dataset %s: Old %s dataset source %s getting overwritten by %s",
                         ds.id,
                         classifier,
                         self.derived_from[ds.id][classifier],
                         src.id)
        self.derived_from[ds.id][classifier] = src.id
        # Add source back-link
        if src.id not in self.derivations:
            self.derivations[src.id] = {}
        if self.derivations[src.id].get(classifier, ds.id) != ds.id:
            _LOG.warning("Dataset %s: Old %s dataset derivation %s getting overwritten by %s",
                         src.id,
                         classifier,
                         self.derivations[src.id][classifier],
                         ds.id)
        self.derivations[src.id][classifier] = ds.id

    def search_product_duplicates(self,
                                  product: Product,
                                  *args: Union[str, Field]
                                  ) -> Iterable[Tuple[Tuple, Iterable[UUID]]]:
        GroupedVals = namedtuple('search_result', args)

        def to_field(f: Union[str, Field]) -> Field:
            if isinstance(f, str):
                return product.metadata_type.dataset_fields[f]
            assert isinstance(f, fields.Field), "Not a field: %r" % (f,)
            return f

        fields = [to_field(f) for f in args]

        def values(ds: Dataset) -> GroupedVals:
            vals = []
            for field in fields:
                vals.append(field.extract(ds.metadata_doc))
            return GroupedVals(*vals)

        dups = {}
        for ds in self.active_by_id.values():
            if ds.type.name != product.name:
                continue
            vals = values(ds)
            if vals in dups:
                dups[vals].append(ds.id)
            else:
                dups[vals] = [ds.id]
        return list(dups.items())

    def can_update(self,
                   dataset: Dataset,
                   updates_allowed: Optional[Mapping[Offset, AllowPolicy]] = None
                  ) -> Tuple[bool, Iterable[Change], Iterable[Change]]:
        # Current exactly the same as postgres implementation.  Could be pushed up to base class?
        existing = self.get(dataset.id, include_sources=dataset.sources is not None)
        if not existing:
            raise ValueError(
                f'Unknown dataset {dataset.id}, cannot update - did you intend to add it?'
            )
        if dataset.type.name != existing.type.name:
            raise ValueError(
                f'Changing product is not supported. From {existing.type.name} to {dataset.type.name} in {dataset.id}'
            )
        # TODO: Determine (un)safe changes from metadata type
        allowed = {
            tuple(): changes.allow_extension
        }
        allowed.update(updates_allowed or {})
        doc_changes = get_doc_changes(
            existing.metadata_doc,
            jsonify_document(dataset.metadata_doc)
        )
        good_changes, bad_changes = changes.classify_changes(doc_changes, allowed)
        return not bad_changes, good_changes, bad_changes

    def update(self,
               dataset: Dataset,
               updates_allowed: Optional[Mapping[Offset, AllowPolicy]] = None
              ) -> Dataset:
        existing = self.get(dataset.id)
        if not existing:
            raise ValueError(
                f'Unknown dataset {dataset.id}, cannot update - did you intend to add it?'
            )
        elif existing.is_archived:
            raise ValueError(f"Dataset {dataset.id} is archived.  Please restore before updating.")
        can_update, safe_changes, unsafe_changes = self.can_update(dataset, updates_allowed)
        if not safe_changes and not unsafe_changes:
            self._update_locations(dataset, existing)
            _LOG.info("No metadata changes detected for dataset %s", dataset.id)
            return dataset

        for offset, old_val, new_val in safe_changes:
            _LOG.info(
                "Safe metadata changes in %s from %r to %r",
                _readable_offset(offset),
                old_val,
                new_val
            )
        for offset, old_val, new_val in safe_changes:
            _LOG.warning(
                "Unsafe metadata changes in %s from %r to %r",
                _readable_offset(offset),
                old_val,
                new_val
            )

        if not can_update:
            unsafe_txt = ", ".join(_readable_offset(offset) for offset, _, _ in unsafe_changes)
            raise ValueError(f"Unsafe metadata changes in {dataset.id}: {unsafe_txt}")

        # Apply update
        _LOG.info("Updating dataset %s", dataset.id)
        persistable = self.clone(dataset, for_save=True)
        self.by_id[dataset.id] = persistable
        self.active_by_id[dataset.id] = persistable
        return self.get(dataset.id)

    def _update_locations(self,
                          dataset: Dataset,
                          existing: Optional[Dataset] = None
                         ) -> bool:
        skip_set = set([None] + existing.uris if existing is not None else [])
        new_uris = [uri for uri in dataset.uris if uri not in skip_set]
        if len(new_uris):
            _LOG.info("Adding locations for dataset %s: %s", dataset.id, ", ".join(new_uris))
        for uri in reversed(new_uris):
            self.add_location(dataset.id, uri)
        return len(new_uris) > 0

    def archive(self, ids: Iterable[DSID]) -> None:
        for id_ in ids:
            id_ = dsid_to_uuid(id_)
            if id_ in self.active_by_id:
                ds = self.active_by_id.pop(id_)
                self.by_product[ds.type.name] = [i for i in self.by_product[ds.type.name] if i != ds.id]
                ds.archived_time = datetime.datetime.now()
                self.archived_by_id[id_] = ds

    def restore(self, ids: Iterable[DSID]) -> None:
        for id_ in ids:
            id_ = dsid_to_uuid(id_)
            if id_ in self.archived_by_id:
                ds = self.archived_by_id.pop(id_)
                ds.archived_time = None
                self.active_by_id[id_] = ds
                self.by_product[ds.type.name].append(ds.id)

    def purge(self, ids: Iterable[DSID]) -> None:
        for id_ in ids:
            id_ = dsid_to_uuid(id_)
            if id_ in self.archived_by_id:
                del self.archived_by_id[id_]
                del self.by_id[id_]
                if id_ in self.derived_from:
                    for classifier, src_id in self.derived_from[id_].items():
                        del self.derivations[src_id][classifier]
                    del self.derived_from[id_]
                if id_ in self.derivations:
                    for classifier, child_id in self.derivations[id_].items():
                        del self.derived_from[child_id][classifier]
                    del self.derivations[id_]

    def get_all_dataset_ids(self, archived: bool) -> Iterable[UUID]:
        if archived:
            return (id_ for id_ in self.archived_by_id.keys())
        else:
            return (id_ for id_ in self.active_by_id.keys())

    def get_field_names(self, product_name=None) -> Iterable[str]:
        if product_name is None:
            prods = self.product_resource.get_all()
        else:
            prods = [self.product_resource.get_by_name(product_name)]

        out = set()
        for prod in prods:
            out.update(prod.metadata_type.dataset_fields)
        return out

    def get_locations(self, id_: DSID) -> Iterable[str]:
        uuid = dsid_to_uuid(id_)
        return (s for s in self.locations[uuid])

    def get_archived_locations(self, id_: DSID) -> Iterable[str]:
        uuid = dsid_to_uuid(id_)
        return (s for s, dt in self.archived_locations[uuid])

    def get_archived_location_times(self, id_: DSID) -> Iterable[Tuple[str, datetime.datetime]]:
        uuid = dsid_to_uuid(id_)
        return ((s, dt) for s, dt in self.archived_locations[uuid])

    def add_location(self, id_: DSID, uri: str) -> bool:
        uuid = dsid_to_uuid(id_)
        if uuid not in self.by_id:
            warnings.warn(f"dataset {id_} is not an active dataset")
            return False
        if not uri:
            warnings.warn(f"Cannot add empty uri. (dataset {id_})")
            return False
        if uri in self.locations[uuid]:
            return False
        self.locations[uuid].append(uri)
        return True

    def get_datasets_for_location(self, uri: str, mode: Optional[str] = None) -> Iterable[Dataset]:
        if mode is None:
            mode = 'exact' if uri.count('#') > 0 else 'prefix'
        if mode not in ("exact", "prefix"):
            raise ValueError(f"Unsupported query mode: {mode}")
        ids: Set[DSID] = set()
        if mode == "exact":
            test: Callable[[str], bool] = lambda l: l == uri
        else:
            test = lambda l: l.startswith(uri)
        for id_, locs in self.locations.items():
            for loc in locs:
                if test(loc):
                    ids.add(id_)
                    break
        return self.bulk_get(ids)

    def remove_location(self, id_: DSID, uri: str) -> bool:
        uuid = dsid_to_uuid(id_)
        if uuid in self.locations:
            old_locations = self.locations[uuid]
            new_locations = [loc for loc in old_locations if loc != uri]
            if len(new_locations) == len(old_locations):
                return False
            self.locations[uuid] = new_locations
            return True
        if uuid in self.archived_locations:
            old_locations = self.archived_locations[uuid]
            new_locations = [(loc, dt) for loc, dt in old_locations if loc != uri]
            if len(new_locations) == len(old_locations):
                return False
            self.archived_locations[uuid] = new_locations
            return True
        return False

    def archive_location(self, id_: DSID, uri: str) -> bool:
        uuid = dsid_to_uuid(id_)
        if uuid not in self.locations:
            return False
        old_locations = self.locations[uuid]
        new_locations = [loc for loc in old_locations if loc != uri]
        if len(new_locations) == len(old_locations):
            return False
        self.locations[uuid] = new_locations
        self.archived_locations[uuid].append((uri, datetime.datetime.now()))
        return True

    def restore_location(self, id_: DSID, uri: str) -> bool:
        uuid = dsid_to_uuid(id_)
        if uuid not in self.archived_locations:
            return False
        old_locations = self.archived_locations[uuid]
        new_locations = [(loc, dt) for loc, dt in old_locations if loc != uri]
        if len(new_locations) == len(old_locations):
            return False
        self.archived_locations[uuid] = new_locations
        self.locations[uuid].append(uri)
        return True

    def search_by_metadata(self, metadata: Mapping[str, QueryField]):
        for ds in self.active_by_id.values():
            if metadata_subset(metadata, ds.metadata_doc):
                yield ds

    def search(self,
               limit: Optional[int] = None,
               source_filter: Optional[Mapping[str, QueryField]] = None,
               **query: QueryField) -> Iterable[Dataset]:
        def get_prod_queries(**query: QueryField) -> Iterable[Tuple[Mapping[str, QueryField], Product]]:
            return ((q, product) for  product, q in self.product_resource.search_robust(**query))

        if source_filter:
            product_queries = list(get_prod_queries(**source_filter))
            if not product_queries:
                raise ValueError(f"No products match source filter: {source_filter}")
            if len(product_queries) > 1:
                raise RuntimeError(f"Multiproduct source_filters are not supported. Try adding 'product' field.")
            source_queries, source_product = product_queries[0]
            source_exprs = tuple(fields.to_expressions(source_product.metadata_type.dataset_fields.get, **source_queries))
        else:
            source_product = None
            source_exprs = ()
        product_queries = list(get_prod_queries(**query))
        if not product_queries:
            prod_name = query.get('product')
            if prod_name is None:
                raise ValueError(f'No products match search terms: {query}')
            else:
                raise ValueError(f'No such product: {prod_name}')

        matches = 0
        for q, product in product_queries:
            if limit is not None and matches >= limit:
                break
            query_exprs = tuple(fields.to_expressions(product.metadata_type.dataset_fields.get, **q))
            for dsid in self.by_product.get(product.name, []):
                if limit is not None and matches >= limit:
                    break
                ds = self.get(dsid, include_sources=True)
                query_matches = True
                for expr in query_exprs:
                    if not expr.evaluate(ds.metadata_doc):
                        query_matches = False
                        break
                if not query_matches:
                    continue
                if source_product:
                    matching_source = None
                    for sds in ds.sources.values():
                        if sds.type != source_product:
                            continue
                        source_matches = True
                        for expr in source_exprs:
                            if not expr.evaluate(sds.metadata_doc):
                                source_matches = False
                                break
                        if source_matches:
                            matching_source = sds
                            break
                    if not matching_source:
                        continue
                matches += 1
                yield ds

    def search_by_product(self, **query):
        return []

    def search_returning(self, field_names, limit=None, **query):
        return []

    def count(self, **query: QueryField) -> int:
        return len(list(self.search(**query)))

    def count_by_product(self, **query):
        return []

    def count_by_product_through_time(self, period, **query):
        return []

    def count_product_through_time(self, period, **query):
        return []

    def search_summaries(self, **query):
        return []

    def get_product_time_bounds(self, product: str) -> Tuple[datetime.datetime, datetime.datetime]:
        min_time: Optional[datetime.datetime] = None
        max_time: Optional[datetime.datetime] = None
        prod = self.product_resource.get_by_name(product)
        if prod is None:
            raise ValueError(f"Product {product} not in index")
        time_fld = prod.metadata_type.dataset_fields["time"]
        for dsid in self.by_product.get(product, []):
            ds = self.get(dsid)
            dsmin, dsmax = time_fld.extract(ds.metadata_doc)
            if dsmax is None and dsmin is None:
                continue
            elif dsmin is None:
                dsmin = dsmax
            elif dsmax is None:
                dsmax = dsmin
            if min_time is None or dsmin < min_time:
                min_time = dsmin
            if max_time is None or dsmax > max_time:
                max_time = dsmax
        return (min_time, max_time)

    # pylint: disable=redefined-outer-name
    def search_returning_datasets_light(self, field_names: tuple, custom_offsets=None, limit=None, **query):
        return []

    def clone(self, orig: Dataset, for_save=False, lookup_locations=True) -> Dataset:
        if for_save:
            uris = []
        elif lookup_locations:
            uris = self.locations[orig.id].copy()
        else:
            uris = orig.uris.copy()
        return Dataset(
            type_=self.product_resource.clone(orig.type),
            metadata_doc=jsonify_document(orig.metadata_doc_without_lineage()),
            uris=uris,
            indexed_by="user" if for_save and orig.indexed_by is None else orig.indexed_by,
            indexed_time=datetime.datetime.now() if for_save and orig.indexed_time is None else orig.indexed_time,
            archived_time=None if for_save else orig.archived_time
        )
