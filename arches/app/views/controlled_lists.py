import logging
from collections import defaultdict
from datetime import datetime
from uuid import UUID

from django.contrib.postgres.expressions import ArraySubquery
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, OuterRef
from django.views.generic import View
from django.utils.decorators import method_decorator
from django.utils.translation import get_language, gettext as _

from arches.app.models.models import (
    ControlledList,
    ControlledListItem,
    ControlledListItemImage,
    ControlledListItemImageMetadata,
    ControlledListItemValue,
    Language,
    Node,
)
from arches.app.utils.betterJSONSerializer import JSONDeserializer
from arches.app.utils.decorators import group_required
from arches.app.utils.permission_backend import get_nodegroups_by_perm
from arches.app.utils.response import JSONErrorResponse, JSONResponse
from arches.app.utils.string_utils import str_to_bool


logger = logging.getLogger(__name__)

class MixedListsException(Exception):
    pass


def serialize(obj, depth_map=None, flat=False):
    """
    This is a recursive function. The first caller (you) doesn't need
    to provide a `depth_map`, but the recursive calls (see below) do.

    flat=False provides a tree representation.
    flat=True is just that, flat (used in reference datatype widget).
    """
    if depth_map is None:
        depth_map = defaultdict(int)
    match obj:
        case ControlledList():
            data = {
                "id": str(obj.id),
                "name": obj.name,
                "dynamic": obj.dynamic,
                "search_only": obj.search_only,
                "items": sorted(
                    [
                        serialize(item, depth_map, flat)
                        for item in obj.controlled_list_items.all()
                        if flat or item.parent_id is None
                    ],
                    key=lambda d: d["sortorder"],
                ),
            }
            if hasattr(obj, "node_ids"):
                data["nodes"] = [
                    {
                        "id": node_id,
                        "name": node_name,
                        "nodegroup_id": nodegroup_id,
                        "graph_id": graph_id,
                        "graph_name": graph_name,
                    }
                    for node_id, node_name, nodegroup_id, graph_id, graph_name in zip(
                        obj.node_ids,
                        obj.node_names,
                        obj.nodegroup_ids,
                        obj.graph_ids,
                        obj.graph_names,
                        strict=True,
                    )
                ]
            else:
                data["nodes"] = [
                    {
                        "id": str(node.pk),
                        "name": node.name,
                        "nodegroup_id": node.nodegroup_id,
                        "graph_id": node.graph_id,
                        "graph_name": str(node.graph.name),
                    }
                    for node in Node.with_controlled_list.filter(
                        controlled_list=obj.pk,
                        source_identifier=None,
                    ).select_related("graph")
                ]
            return data
        case ControlledListItem():
            if obj.parent_id:
                depth_map[obj.id] = depth_map[obj.parent_id] + 1
            data = {
                "id": str(obj.id),
                "controlled_list_id": str(obj.controlled_list_id),
                "uri": obj.uri,
                "sortorder": obj.sortorder,
                "guide": obj.guide,
                "values": [
                    serialize(value, depth_map)
                    for value in obj.controlled_list_item_values.all()
                    if value.valuetype_id != "image"
                ],
                "images": [
                    serialize(image, depth_map)
                    for image in obj.controlled_list_item_images.all()
                ],
                "parent_id": str(obj.parent_id) if obj.parent_id else None,
                "depth": depth_map[obj.id],
            }
            if not flat:
                data["children"] = sorted(
                    [serialize(child, depth_map, flat) for child in obj.children.all()],
                    key=lambda d: d["sortorder"],
                )
            return data
        case ControlledListItemValue():
            return {
                "id": str(obj.id),
                "valuetype_id": obj.valuetype_id,
                "language_id": obj.language_id,
                "value": obj.value,
                "item_id": obj.controlled_list_item_id,
            }
        case ControlledListItemImage():
            return {
                "id": str(obj.id),
                "item_id": obj.controlled_list_item_id,
                "url": obj.value.url,
                "metadata": [
                    serialize(metadata, depth_map)
                    for metadata in obj.controlled_list_item_image_metadata.all()
                ]
            }
        case ControlledListItemImageMetadata():
            choices = ControlledListItemImageMetadata.MetadataChoices
            return {
                field: str(value)
                for (field, value) in vars(obj).items() if not field.startswith("_")
            } | {
                # Get localized label for metadata type
                "metadata_label": str(choices(obj.metadata_type).label)
            }


def prefetch_terms(request):
    """Children at arbitrary depth will still be returned, but tell
    the ORM to prefetch a certain depth to mitigate N+1 queries after."""
    find_children = not str_to_bool(request.GET.get("flat", "false"))

    # Raising the prefetch depth will only save queries, never cause more.
    # Might add slight python overhead? ~12-14 is enough for Getty AAT.
    prefetch_depth = 14

    terms = []
    for i in range(prefetch_depth):
        if i == 0:
            terms.extend(
                [
                    "controlled_list_items",
                    "controlled_list_items__controlled_list_item_values",
                    "controlled_list_items__controlled_list_item_images",
                    "controlled_list_items__controlled_list_item_images__controlled_list_item_image_metadata",
                ]
            )
        elif find_children:
            terms.extend(
                [
                    f"controlled_list_items{'__children' * i}",
                    f"controlled_list_items{'__children' * i}__controlled_list_item_values",
                    f"controlled_list_items{'__children' * i}__controlled_list_item_images",
                    f"controlled_list_items{'__children' * i}__controlled_list_item_images__controlled_list_item_image_metadata",
                ]
            )
    return terms


def handle_items(item_dicts, max_sortorder=-1):
    items_to_save = []
    values_to_save = []
    image_metadata_to_save = []

    def handle_item(item_dict):
        nonlocal items_to_save
        nonlocal values_to_save
        nonlocal image_metadata_to_save
        nonlocal max_sortorder

        values = item_dict.pop("values")
        images = item_dict.pop("images")
        # Altering hierarchy is done by altering parents.
        children = item_dict.pop("children", None)
        item_dict.pop("depth", None)

        item_to_save = ControlledListItem(**item_dict)
        items_to_save.append(item_to_save)

        if item_to_save.sortorder < 0:
            item_to_save.sortorder = max_sortorder + 1

        if len({item.controlled_list_id for item in items_to_save}) > 1:
            raise MixedListsException

        for value in values:
            value.pop("item_id")  # trust the item, not the label
            value_to_save = ControlledListItemValue(
                controlled_list_item_id=UUID(item_to_save.id),
                **value,
            )
            value_to_save._state.adding = False  # allows checking uniqueness
            value_to_save.full_clean()
            values_to_save.append(value_to_save)

        for image in images:
            for metadata in image["metadata"]:
                metadata.pop("controlled_list_item_image_id")
                metadata.pop("metadata_label", None)  # computed by serialize()
                metadata_to_save = ControlledListItemImageMetadata(
                    controlled_list_item_image_id=UUID(image["id"]),
                    **metadata,
                )
                metadata_to_save._state.adding = False  # allows checking uniqueness
                metadata_to_save.full_clean()
                image_metadata_to_save.append(metadata_to_save)

        # Recurse
        for child in children:
            handle_item(child)

    for item_dict in item_dicts:
        handle_item(item_dict)

    for item_to_save in items_to_save:
        item_to_save._state.adding = False  # allows checking uniqueness
        item_to_save.full_clean(validate_constraints=False)
        # Sortorder uniqueness is deferred.
        item_to_save.validate_constraints(exclude=["sortorder"])

    ControlledListItem.objects.bulk_update(
        items_to_save, fields=["controlled_list_id", "guide", "uri", "sortorder", "parent"]
    )
    ControlledListItemValue.objects.bulk_update(
        values_to_save, fields=["value", "valuetype", "language"]
    )
    ControlledListItemImageMetadata.objects.bulk_update(
        image_metadata_to_save, fields=["value", "metadata_type", "language"]
    )


@method_decorator(
    group_required("RDM Administrator", raise_exception=True), name="dispatch"
)
class ControlledListsView(View):
    def get(self, request):
        """Returns either a flat representation (?flat=true) or a tree (default)."""
        lists = (
            ControlledList.objects.all()
            .annotate(node_ids=self.node_subquery())
            .annotate(node_names=self.node_subquery("name"))
            .annotate(nodegroup_ids=self.node_subquery("nodegroup_id"))
            .annotate(graph_ids=self.node_subquery("graph_id"))
            .annotate(graph_names=self.node_subquery("graph__name"))
            .order_by("name")
            .prefetch_related(*prefetch_terms(request))
        )
        serialized_lists = [
            serialize(obj, flat=str_to_bool(request.GET.get("flat", "false")))
            for obj in lists
        ]
        filtered = self.filter_permitted_nodegroups(serialized_lists, request)
        data = {"controlled_lists": filtered}

        return JSONResponse(data)

    @staticmethod
    def node_subquery(node_field: str = "pk"):
        return ArraySubquery(
            Node.with_controlled_list
            .filter(controlled_list=OuterRef("id"), source_identifier=None)
            .select_related("graph" if node_field.startswith("graph__") else None)
            .order_by("pk")
            .values(node_field)
        )

    def filter_permitted_nodegroups(self, serialized_lists, request):
        permitted_nodegroups = [
            ng.pk for ng in get_nodegroups_by_perm(request.user, "read_nodegroup")
        ]

        for lst in serialized_lists:
            lst["nodes"] = [
                node_dict
                for node_dict in lst["nodes"]
                if node_dict["nodegroup_id"] in permitted_nodegroups
            ]
        return serialized_lists


@method_decorator(
    group_required("RDM Administrator", raise_exception=True), name="dispatch"
)
class ControlledListView(View):
    def get(self, request, **kwargs):
        """Returns either a flat representation (?flat=true) or a tree (default)."""
        list_id = kwargs.get("id")
        try:
            lst = ControlledList.objects.prefetch_related(*prefetch_terms(request)).get(
                pk=list_id
            )
        except ControlledList.DoesNotExist:
            return JSONErrorResponse(status=404)
        return JSONResponse(
            serialize(lst, flat=str_to_bool(request.GET.get("flat", "false")))
        )

    def post(self, request, **kwargs):
        if not (list_id := kwargs.get("id", None)):
            # Add a new list.
            lst = ControlledList(
                name=_("Untitled List: ")
                + datetime.now().isoformat(sep=" ", timespec="seconds")
            )
            lst.save()
            return JSONResponse(serialize(lst), status=201)

        data = JSONDeserializer().deserialize(request.body)

        qs = (
            ControlledList.objects.filter(pk=list_id)
            .annotate(max_sortorder=Max(
                "controlled_list_items__sortorder", default=-1
            ))
        )

        try:
            with transaction.atomic():
                try:
                    clist = qs.get()
                except ControlledList.DoesNotExist:
                    return JSONErrorResponse(status=404)

                clist.dynamic = data["dynamic"]
                clist.search_only = data["search_only"]
                clist.name = data["name"]
                clist.full_clean()

                handle_items(data["items"], max_sortorder=clist.max_sortorder)

                clist.save()
        except ValidationError as ve:
            return JSONErrorResponse(message=" ".join(ve.messages), status=400)
        except MixedListsException:
            return JSONErrorResponse(message=_("Items must belong to the same list."), status=400)
        except:
            return JSONErrorResponse()

        return JSONResponse(serialize(clist))

    def delete(self, request, **kwargs):
        list_id: UUID = kwargs.get("id")
        for node in Node.with_controlled_list.filter(controlled_list=list_id):
            try:
                lst = ControlledList.objects.get(id=list_id)
            except ControlledList.DoesNotExist:
                return JSONErrorResponse(status=404)
            return JSONErrorResponse(
                message=_(
                    "{controlled_list} could not be deleted: still in use by {graph} - {node}".format(
                        controlled_list=lst.name,
                        graph=node.graph.name,
                        node=node.name,
                    )
                ),
                status=400,
            )
        objs_deleted, unused = ControlledList.objects.filter(pk=list_id).delete()
        if not objs_deleted:
            return JSONErrorResponse(status=404)
        return JSONResponse(status=204)


@method_decorator(
    group_required("RDM Administrator", raise_exception=True), name="dispatch"
)
class ControlledListItemView(View):
    def add_new_item(self, request):
        data = JSONDeserializer().deserialize(request.body)
        parent_id = data["parent_id"]

        # The front end shows lists and items in the same tree, and when
        # sending the parent id to the endpoint, it doesn't really know
        # if the parent is a list or an item. The backend does care whether
        # the parent is a list or an item, so we figure it out here.
        try:
            controlled_list_id = ControlledListItem.objects.get(
                pk=parent_id
            ).controlled_list_id
        except ControlledListItem.DoesNotExist:
            controlled_list_id = parent_id

        try:
            with transaction.atomic():
                controlled_list = (
                    ControlledList.objects.filter(pk=controlled_list_id)
                    .annotate(
                        max_sortorder=Max(
                            "controlled_list_items__sortorder", default=-1
                        )
                    )
                    .get()
                )
                item = ControlledListItem.objects.create(
                    controlled_list=controlled_list,
                    sortorder=controlled_list.max_sortorder + 1,
                    parent_id=None if controlled_list_id == parent_id else parent_id,
                )
                ControlledListItemValue.objects.create(
                    controlled_list_item=item,
                    value=_("New Item: ")
                    + datetime.now().isoformat(sep=" ", timespec="seconds"),
                    valuetype_id="prefLabel",
                    # todo: update with capitalize_region()
                    language_id=get_language(),
                )
        except ControlledList.DoesNotExist:
            return JSONErrorResponse(status=404)
        except Exception as e:
            logger.error(e)
            return JSONErrorResponse()

        return JSONResponse(serialize(item), status=201)

    def post(self, request, **kwargs):
        if not (item_id := kwargs.get("id", None)):
            return self.add_new_item(request)

        # Update list item
        data = JSONDeserializer().deserialize(request.body)

        try:
            controlled_list = (
                ControlledList.objects.filter(pk=data["controlled_list_id"])
                .annotate(
                    max_sortorder=Max(
                        "controlled_list_items__sortorder", default=-1
                    )
                )
                .get()
            )
        except ControlledList.DoesNotExist:
            return JSONErrorResponse(status=404)

        try:
            with transaction.atomic():
                for item in ControlledListItem.objects.filter(
                    pk=item_id
                ).select_for_update():
                    handle_items([data], max_sortorder=controlled_list.max_sortorder)
                    break
                else:
                    return JSONErrorResponse(status=404)
                serialized_item = serialize(item)

        except ValidationError as ve:
            return JSONErrorResponse(message=" ".join(ve.messages), status=400)
        except MixedListsException:
            return JSONErrorResponse(message=_("Items must belong to the same list."), status=400)
        except RecursionError:
            return JSONErrorResponse(message=_("Recursive structure detected."), status=400)
        except:
            return JSONErrorResponse()

        return JSONResponse(serialized_item)

    def delete(self, request, **kwargs):
        item_id = kwargs.get("id")
        objs_deleted, unused = ControlledListItem.objects.filter(pk=item_id).delete()
        if not objs_deleted:
            return JSONErrorResponse(status=404)
        return JSONResponse(status=204)


@method_decorator(
    group_required("RDM Administrator", raise_exception=True), name="dispatch"
)
class ControlledListItemValueView(View):
    def add_new_value(self, request):
        data = JSONDeserializer().deserialize(request.body)

        value = ControlledListItemValue(
            controlled_list_item_id=UUID(data["item_id"]),
            valuetype_id=data["valuetype_id"],
            language_id=data["language_id"],
            value=data["value"],
        )
        try:
            value.full_clean()
        except ValidationError as ve:
            return JSONErrorResponse(message=" ".join(ve.messages), status=400)
        except:
            return JSONErrorResponse()
        value.save()

        return JSONResponse(serialize(value), status=201)

    def post(self, request, **kwargs):
        if not (value_id := kwargs.get("id", None)):
            return self.add_new_value(request)

        data = JSONDeserializer().deserialize(request.body)

        try:
            value = ControlledListItemValue.values_without_images.get(pk=value_id)
        except ControlledListItemValue.DoesNotExist:
            return JSONErrorResponse(status=404)

        value.value = data["value"]
        value.valuetype_id = data["valuetype_id"]
        try:
            value.language = Language.objects.get(code=data["language_id"])
        except Language.DoesNotExist:
            return JSONErrorResponse(status=404)

        try:
            value.full_clean()
        except ValidationError as ve:
            return JSONErrorResponse(message=" ".join(ve.messages), status=400)
        except:
            return JSONErrorResponse()
        value.save()

        return JSONResponse(serialize(value))

    def delete(self, request, **kwargs):
        value_id = kwargs.get("id")
        try:
            value = ControlledListItemValue.values_without_images.get(pk=value_id)
        except:
            return JSONErrorResponse(status=404)
        if (
            value.valuetype_id == "prefLabel"
            and len(
                value.controlled_list_item.controlled_list_item_values.filter(
                    valuetype_id="prefLabel"
                )
            )
            < 2
        ):
            return JSONErrorResponse(
                message=_(
                    "Deleting the item's only remaining preferred label is not permitted."
                ),
                status=400,
            )
        value.delete()
        return JSONResponse(status=204)


@method_decorator(
    group_required("RDM Administrator", raise_exception=True), name="dispatch"
)
class ControlledListItemImageView(View):
    def add_new_image(self, request):
        uploaded_file = request.FILES["item_image"]
        img = ControlledListItemImage(
            controlled_list_item_id=UUID(request.POST["item_id"]),
            valuetype_id="image",
            value=uploaded_file,
        )
        try:
            img.full_clean()
        except ValidationError as ve:
            return JSONErrorResponse(message=" ".join(ve.messages), status=400)
        except:
            return JSONErrorResponse()
        img.save()
        return JSONResponse(serialize(img), status=201)

    def post(self, request, **kwargs):
        if not (image_id := kwargs.get("id", None)):
            return self.add_new_image(request)
        raise NotImplementedError

    def delete(self, request, **kwargs):
        image_id = kwargs.get("id")
        count, unused = ControlledListItemImage.objects.filter(pk=image_id).delete()
        if not count:
            return JSONErrorResponse(status=404)
        return JSONResponse(status=204)


@method_decorator(
    group_required("RDM Administrator", raise_exception=True), name="dispatch"
)
class ControlledListItemImageMetadataView(View):
    def add_new_metadata(self, request):
        data = JSONDeserializer().deserialize(request.body)
        data.pop("metadata_label", None)
        metadata = ControlledListItemImageMetadata(**data)
        try:
            metadata.full_clean()
        except ValidationError as ve:
            return JSONErrorResponse(message="\n".join(ve.messages), status=400)
        except:
            return JSONErrorResponse()
        metadata.save()

        return JSONResponse(serialize(metadata), status=201)

    def post(self, request, **kwargs):
        if not (metadata_id := kwargs.get("id", None)):
            return self.add_new_metadata(request)

        data = JSONDeserializer().deserialize(request.body)

        try:
            metadata = ControlledListItemImageMetadata.objects.get(pk=metadata_id)
        except ControlledListItemImageMetadata.DoesNotExist:
            return JSONErrorResponse(status=404)

        metadata.value = data["value"]
        try:
            metadata.language = Language.objects.get(code=data["language_id"])
        except Language.DoesNotExist:
            return JSONErrorResponse(status=404)
        metadata.metadata_type=data["metadata_type"]

        try:
            metadata.full_clean()
        except ValidationError as ve:
            return JSONErrorResponse(message="\n".join(ve.messages), status=400)
        except:
            return JSONErrorResponse()
        metadata.save()

        return JSONResponse(serialize(metadata))

    def delete(self, request, **kwargs):
        metadata_id = kwargs.get("id")
        count, unused = ControlledListItemImageMetadata.objects.filter(pk=metadata_id).delete()
        if not count:
            return JSONErrorResponse(status=404)
        return JSONResponse(status=204)
