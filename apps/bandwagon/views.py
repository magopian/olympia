import functools

from django import http
from django.db.models import Q
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect

import commonware.log
import jingo
from tower import ugettext_lazy as _lazy, ugettext as _

import amo.utils
from amo.decorators import login_required, post_required
from amo.urlresolvers import reverse
from access import acl
from addons.models import Addon
from addons.views import BaseFilter
from tags.models import Tag
from translations.query import order_by_translation
from .models import (Collection, CollectionAddon,
                     CollectionVote, SPECIAL_SLUGS)
from . import forms

log = commonware.log.getLogger('z.collections')


def get_collection(request, username, slug):
    if (slug in SPECIAL_SLUGS.values() and request.user.is_authenticated()
        and request.amo_user.nickname == username):
        return getattr(request.amo_user, slug + '_collection')()
    else:
        return get_object_or_404(Collection.objects,
                                 author__nickname=username, slug=slug)


def owner_required(f=None, require_owner=True):
    """Requires collection to be owner, by someone."""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(request, username, slug, *args, **kw):
            collection = get_collection(request, username, slug)
            if acl.check_collection_ownership(request, collection,
                                              require_owner=require_owner):
                return func(request, collection, username, slug, *args, **kw)
            else:
                return http.HttpResponseForbidden(
                        _("This is not the collection you are looking for."))
        return wrapper

    if f:
        return decorator(f)
    else:
        return decorator


def legacy_redirect(request, uuid):
    # Nicknames have a limit of 30, so len == 36 implies a uuid.
    key = 'uuid' if len(uuid) == 36 else 'nickname'
    c = get_object_or_404(Collection.objects, **{key: uuid})
    return redirect(c.get_url_path())


def legacy_directory_redirects(request, page):
    sorts = {'editors_picks': 'featured', 'popular': 'popular'}
    loc = base = reverse('collections.list')
    if page in sorts:
        loc = amo.utils.urlparams(base, sort=sorts[page])
    elif request.user.is_authenticated():
        if page == 'mine':
            loc = reverse('collections.user', args=[request.amo_user.nickname])
        elif page == 'favorites':
            loc = reverse('collections.detail',
                          args=[request.amo_user.nickname, 'favorites'])
    return redirect(loc)


class CollectionFilter(BaseFilter):
    opts = (('featured', _lazy('Featured')),
            ('popular', _lazy('Popular')),
            ('rating', _lazy('Highest Rated')),
            ('created', _lazy('Recently Added')))

    def filter(self, field):
        qs = self.base_queryset
        if field == 'featured':
            return qs.filter(type=amo.COLLECTION_FEATURED)
        elif field == 'followers':
            return qs.order_by('-weekly_subscribers')
        elif field == 'rating':
            return qs.order_by('-rating')
        else:
            return qs.order_by('-created')


def collection_listing(request, base=None, extra={}):
    if base is None:
        base = Collection.objects.listed()
    app = Q(application=request.APP.id) | Q(application=None)
    base = base.filter(app)
    filter = CollectionFilter(request, base, key='sort', default='popular')
    collections = amo.utils.paginate(request, filter.qs)
    votes = get_votes(request, collections.object_list)
    return jingo.render(request, 'bandwagon/collection_listing.html',
                        dict(collections=collections, filter=filter,
                             collection_votes=votes, **extra))


def get_votes(request, collections):
    if not request.user.is_authenticated():
        return {}
    q = CollectionVote.objects.filter(
        user=request.amo_user, collection__in=[c.id for c in collections])
    return dict((v.collection_id, v) for v in q)


def user_listing(request, username):
    qs = Collection.objects.filter(author__username=username)
    if not (request.user.is_authenticated() and
            request.amo_user.username == username):
        qs = qs.filter(listed=True)
    return collection_listing(request, qs, extra={'userpage': username})


class CollectionAddonFilter(BaseFilter):
    opts = (('added', _lazy('Added')),
            ('popular', _lazy('Popularity')),
            ('name', _lazy('Name')))

    def filter(self, field):
        if field == 'added':
            return self.base_queryset.order_by('collectionaddon__created')
        elif field == 'name':
            return order_by_translation(self.base_queryset, 'name')
        elif field == 'popular':
            return (self.base_queryset.order_by('-weekly_downloads')
                    .with_index(addons='downloads_type_idx'))


def collection_detail(request, username, slug):
    c = get_collection(request, username, slug)
    if not (c.listed or acl.check_collection_ownership(request, c)):
        return http.HttpResponseForbidden()
    STATUS = amo.VALID_STATUSES
    base = Addon.objects.listed(request.APP, *STATUS) & c.addons.all()
    filter = CollectionAddonFilter(request, base,
                                   key='sort', default='popular')
    notes = get_notes(c)
    # Go directly to CollectionAddon for the count to avoid joins.
    count = CollectionAddon.objects.filter(
        Addon.objects.valid_q(STATUS, prefix='addon__'), collection=c.id)
    addons = amo.utils.paginate(request, filter.qs, per_page=15,
                                count=count.count())

    if c.author_id:
        qs = Collection.objects.listed().filter(author=c.author)
        others = amo.utils.randslice(qs, limit=4, exclude=c.id)
    else:
        others = []

    perms = {
        'view_stats': acl.check_ownership(request, c, require_owner=False),
    }

    tag_ids = c.top_tags
    tags = Tag.objects.filter(id__in=tag_ids) if tag_ids else []
    return jingo.render(request, 'bandwagon/collection_detail.html',
                        {'collection': c, 'filter': filter,
                         'addons': addons, 'notes': notes,
                         'author_collections': others, 'tags': tags,
                         'perms': perms})


def get_notes(collection):
    # This might hurt in a big collection with lots of notes.
    # It's a generator so we don't evaluate anything by default.
    notes = CollectionAddon.objects.filter(collection=collection,
                                           comments__isnull=False)
    rv = {}
    for note in notes:
        rv[note.addon_id] = note.comments
    yield rv


@login_required
def collection_vote(request, username, slug, direction):
    c = get_collection(request, username, slug)
    if request.method != 'POST':
        return redirect(c.get_url_path())

    vote = {'up': 1, 'down': -1}[direction]
    cv, new = CollectionVote.objects.get_or_create(
        collection=c, user=request.amo_user, defaults={'vote': vote})

    if not new:
        if cv.vote == vote:  # Double vote => cancel.
            cv.delete()
        else:
            cv.vote = vote
            cv.save()

    if request.is_ajax():
        return http.HttpResponse()
    else:
        return redirect(c.get_url_path())


def initial_data_from_request(request):
    return dict(author=request.amo_user, application_id=request.APP.id)


@login_required
def add(request):
    "Displays/processes a form to create a collection."
    data = {}
    if request.method == 'POST':
        form = forms.CollectionForm(
                request.POST, request.FILES,
                initial=initial_data_from_request(request))
        aform = forms.AddonsForm(request.POST)
        if form.is_valid():
            collection = form.save()
            if aform.is_valid():
                aform.save(collection)
            log.info('Created collection %s' % collection.id)
            return http.HttpResponseRedirect(collection.get_url_path())
        else:
            data['addons'] = aform.clean_addon()
            data['comments'] = aform.clean_addon_comment()
    else:
        form = forms.CollectionForm()

    data['form'] = form
    return jingo.render(request, 'bandwagon/add.html', data)


def ajax_new(request):
    form = forms.CollectionForm(request.POST or None,
        initial={'author': request.amo_user,
                 'application_id': request.APP.id},
    )

    if request.method == 'POST':

        if form.is_valid():
            collection = form.save()
            addon_id = request.REQUEST['addon_id']
            a = Addon.objects.get(pk=addon_id)
            collection.add_addon(a)
            log.info('Created collection %s' % collection.id)
            return http.HttpResponseRedirect(reverse('collections.ajax_list')
                                             + '?addon_id=%s' % addon_id)

    return jingo.render(request, 'bandwagon/ajax_new.html', {'form': form})


@login_required
def ajax_list(request):
    # Get collections associated with this user
    collections = request.amo_user.collections.manual()
    addon_id = int(request.GET['addon_id'])

    for collection in collections:
        # See if the collections contains the addon
        if addon_id in collection.addons.values_list('id', flat=True):
            collection.has_addon = True

    return jingo.render(request, 'bandwagon/ajax_list.html',
                {'collections': collections})


@login_required
@post_required
def collection_alter(request, username, slug, action):
    c = get_collection(request, username, slug)
    return change_addon(request, c, action)


def change_addon(request, collection, action):
    if not acl.check_collection_ownership(request, collection):
        return http.HttpResponseForbidden()

    try:
        addon = get_object_or_404(Addon.objects, pk=request.POST['addon_id'])
    except (ValueError, KeyError):
        return http.HttpResponseBadRequest()

    getattr(collection, action + '_addon')(addon)
    log.info('%s: %s %s to collection %s' %
             (request.amo_user, action, addon.id, collection.id))

    if request.is_ajax():
        url = '%s?addon_id=%s' % (reverse('collections.ajax_list'), addon.id)
    else:
        url = collection.get_url_path()
    return redirect(url)


@login_required
@post_required
def ajax_collection_alter(request, action):
    try:
        c = get_object_or_404(Collection.objects, pk=request.POST['id'])
    except (ValueError, KeyError):
        return http.HttpResponseBadRequest()
    return change_addon(request, c, action)


@login_required
@owner_required
def edit(request, collection, username, slug):
    is_admin = acl.action_allowed(request, 'Admin', '%')

    if request.method == 'POST':
        form = forms.CollectionForm(request.POST, request.FILES,
                                    initial=initial_data_from_request(request),
                                    instance=collection)
        if form.is_valid():
            collection = form.save()
            log.info('%s edited collection %s' %
                     (request.amo_user, collection.id))
            return http.HttpResponseRedirect(collection.get_url_path())
    else:
        form = forms.CollectionForm(instance=collection)

    addons = collection.addons.all()
    comments = get_notes(collection).next()

    if is_admin:
        initial = dict(type=collection.type,
                       application=collection.application_id)
        admin_form = forms.AdminForm(initial=initial)
    else:
        admin_form = None

    data = dict(collection=collection,
                form=form,
                user=request.amo_user,
                username=username,
                slug=slug,
                is_admin=is_admin,
                admin_form=admin_form,
                addons=addons,
                comments=comments)
    return jingo.render(request, 'bandwagon/edit.html', data)


@login_required
@owner_required(require_owner=False)
def edit_addons(request, collection, username, slug):
    if request.method == 'POST':
        form = forms.AddonsForm(request.POST)
        if form.is_valid():
            form.save(collection)
            log.info('%s added add-ons to %s' %
                     (request.amo_user, collection.id))
            return http.HttpResponseRedirect(collection.get_url_path())

    collection_addons = collection.collectionaddon_set.all()
    addons = []
    comments = {}

    for ca in collection_addons:
        comments[ca.addon_id] = ca.comments
        addons.append(ca.addon)

    data = dict(collection=collection, username=username, slug=slug,
                addons=addons, comments=comments,
                form=forms.CollectionForm(instance=collection))
    return jingo.render(request, 'bandwagon/edit.html', data)


@login_required
@owner_required
def edit_contributors(request, collection, username, slug):
    is_admin = acl.action_allowed(request, 'Admin', '%')

    data = dict(collection=collection, username=username, slug=slug,
                is_admin=is_admin)

    if is_admin:
        initial = dict(type=collection.type,
                       application=collection.application_id)
        data['admin_form'] = forms.AdminForm(initial=initial)

    if request.method == 'POST':
        if is_admin:
            admin_form = forms.AdminForm(request.POST)
            if admin_form.is_valid():
                admin_form.save(collection)

        form = forms.ContributorsForm(request.POST)
        if form.is_valid():
            form.save(collection)
            messages.success(request, _('Your collection has been updated.'))
            if form.cleaned_data['new_owner']:
                return http.HttpResponseRedirect(collection.get_url_path())
            return http.HttpResponseRedirect(
                    reverse('collections.edit_contributors',
                            args=[username, slug]))

    data['form'] = forms.CollectionForm(instance=collection)
    return jingo.render(request, 'bandwagon/edit.html', data)


@login_required
@owner_required
@post_required
def edit_privacy(request, collection, username, slug):
    collection.listed = not collection.listed
    collection.save()
    log.info('%s changed privacy on collection %s' %
             (request.amo_user, collection.id))
    return redirect(collection.get_url_path())


@login_required
def delete(request, username, slug):
    collection = get_object_or_404(Collection, author__nickname=username,
                                   slug=slug)

    if not acl.check_collection_ownership(request, collection, True):
        log.info('%s is trying to delete collection %s'
                 % (request.amo_user, collection.id))
        return http.HttpResponseForbidden(
                _('This is not the collection you are looking for.'))

    data = dict(collection=collection, username=username, slug=slug)

    if request.method == 'POST':
        if request.POST['sure'] == '1':
            collection.delete()
            log.info('%s deleted collection %s' %
                     (request.amo_user, collection.id))
            url = reverse('collections.user', args=[username])
            return http.HttpResponseRedirect(url)
        else:
            return http.HttpResponseRedirect(collection.get_url_path())

    return jingo.render(request, 'bandwagon/delete.html', data)
