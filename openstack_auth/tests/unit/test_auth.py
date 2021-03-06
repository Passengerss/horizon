# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import uuid

from django.conf import settings
from django.contrib import auth
from django import test
from django.test.utils import override_settings
from django.urls import reverse
from keystoneauth1 import exceptions as keystone_exceptions
from keystoneauth1.identity import v3 as v3_auth
from keystoneauth1 import session
from keystoneauth1 import token_endpoint
from keystoneclient.v3 import client as client_v3
from keystoneclient.v3 import projects
import mock
from mox3 import mox

from openstack_auth.plugin import password
from openstack_auth.tests import data_v3
from openstack_auth import utils


DEFAULT_DOMAIN = settings.OPENSTACK_KEYSTONE_DEFAULT_DOMAIN


# NOTE(e0ne): it's copy-pasted from horizon.test.helpers module until we
# figure out how to avoid this.
class IsA(object):
    """Class to compare param is a specified class."""
    def __init__(self, cls):
        self.cls = cls

    def __eq__(self, other):
        return isinstance(other, self.cls)


class OpenStackAuthTestsMixin(object):
    """Common functions for version specific tests."""

    scenarios = [
        ('pure', {'interface': None}),
        ('public', {'interface': 'publicURL'}),
        ('internal', {'interface': 'internalURL'}),
        ('admin', {'interface': 'adminURL'})
    ]

    def get_form_data(self, user):
        return {'region': "default",
                'domain': DEFAULT_DOMAIN,
                'password': user.password,
                'username': user.name}


class OpenStackAuthTestsV3(OpenStackAuthTestsMixin,
                           test.TestCase):

    def setUp(self):
        super(OpenStackAuthTestsV3, self).setUp()

        if getattr(self, 'interface', None):
            override = self.settings(OPENSTACK_ENDPOINT_TYPE=self.interface)
            override.enable()
            self.addCleanup(override.disable)

        self.mox = mox.Mox()
        self.addCleanup(self.mox.VerifyAll)
        self.addCleanup(self.mox.UnsetStubs)

        self.data = data_v3.generate_test_data()
        self.ks_client_module = client_v3
        settings.OPENSTACK_API_VERSIONS['identity'] = 3
        settings.OPENSTACK_KEYSTONE_URL = "http://localhost/identity/v3"

        self.mox.StubOutClassWithMocks(token_endpoint, 'Token')
        self.mox.StubOutClassWithMocks(v3_auth, 'Token')
        self.mox.StubOutClassWithMocks(v3_auth, 'Password')
        self.mox.StubOutClassWithMocks(client_v3, 'Client')
        self.mox.StubOutClassWithMocks(v3_auth, 'Keystone2Keystone')

    def test_switch_keystone_provider_remote_fail(self):
        target_provider = 'k2kserviceprovider'
        self.data = data_v3.generate_test_data(service_providers=True)
        self.sp_data = data_v3.generate_test_data(endpoint='http://sp2')
        projects = [self.data.project_one, self.data.project_two]
        user = self.data.user
        form_data = self.get_form_data(user)

        plugin = v3_auth.Password(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            password=self.data.user.password,
            username=self.data.user.name,
            user_domain_name=DEFAULT_DOMAIN,
            unscoped=True)
        plugin.get_access(mox.IsA(session.Session)). \
            AndReturn(self.data.unscoped_access_info)
        plugin.auth_url = settings.OPENSTACK_KEYSTONE_URL
        client = self.ks_client_module.Client(
            session=mox.IsA(session.Session), auth=plugin)

        plugin = v3_auth.Token(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            token=self.data.unscoped_access_info.auth_token,
            domain_name=DEFAULT_DOMAIN,
            reauthenticate=False)
        plugin.get_access(mox.IsA(session.Session)).AndReturn(
            self.data.domain_scoped_access_info)

        client.projects = self.mox.CreateMockAnything()
        client.projects.list(user=user.id).AndReturn(projects)

        plugin = v3_auth.Token(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            token=self.data.unscoped_access_info.auth_token,
            project_id=self.data.project_one.id,
            reauthenticate=False)
        self.scoped_token_auth = plugin
        plugin.get_access(mox.IsA(session.Session)).AndReturn(
            self.data.unscoped_access_info)
        self.ks_client_module.Client(
            session=mox.IsA(session.Session),
            auth=plugin)

        # mock switch
        plugin = v3_auth.Token(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            token=self.data.unscoped_access_info.auth_token,
            project_id=None,
            reauthenticate=False)
        plugin.get_access(mox.IsA(session.Session)).AndReturn(
            self.data.unscoped_access_info)
        plugin.auth_url = settings.OPENSTACK_KEYSTONE_URL
        client = self.ks_client_module.Client(session=mox.IsA(session.Session),
                                              auth=plugin)

        client.projects = self.mox.CreateMockAnything()
        client.projects.list(user=user.id).AndReturn(projects)

        plugin = v3_auth.Token(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            token=self.data.unscoped_access_info.auth_token,
            project_id=self.data.project_one.id,
            reauthenticate=False)
        plugin.get_access(mox.IsA(session.Session)).AndReturn(
            settings.OPENSTACK_KEYSTONE_URL)
        plugin.get_sp_auth_url(
            mox.IsA(session.Session), target_provider
        ).AndReturn('https://k2kserviceprovider/sp_url')

        # let the K2K plugin fail when logging in
        plugin = v3_auth.Keystone2Keystone(
            base_plugin=plugin, service_provider=target_provider)
        plugin.get_access(mox.IsA(session.Session)).AndRaise(
            keystone_exceptions.AuthorizationFailure)
        self.mox.ReplayAll()

        # Log in
        url = reverse('login')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        response = self.client.post(url, form_data)
        self.assertRedirects(response, settings.LOGIN_REDIRECT_URL)

        # Switch
        url = reverse('switch_keystone_provider', args=[target_provider])
        form_data['keystone_provider'] = target_provider
        response = self.client.get(url, form_data, follow=True)
        self.assertRedirects(response, settings.LOGIN_REDIRECT_URL)

        # Assert that provider has not changed because of failure
        self.assertEqual(self.client.session['keystone_provider_id'],
                         'localkeystone')
        # These should never change
        self.assertEqual(self.client.session['k2k_base_unscoped_token'],
                         self.data.unscoped_access_info.auth_token)
        self.assertEqual(self.client.session['k2k_auth_url'],
                         settings.OPENSTACK_KEYSTONE_URL)

    def test_switch_keystone_provider_remote(self):
        target_provider = 'k2kserviceprovider'
        self.data = data_v3.generate_test_data(service_providers=True)
        self.sp_data = data_v3.generate_test_data(endpoint='http://sp2')
        projects = [self.data.project_one, self.data.project_two]
        domains = []
        user = self.data.user
        form_data = self.get_form_data(user)

        # mock authenticate

        plugin = v3_auth.Password(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            password=self.data.user.password,
            username=self.data.user.name,
            user_domain_name=DEFAULT_DOMAIN,
            unscoped=True)
        plugin.get_access(mox.IsA(session.Session)). \
            AndReturn(self.data.unscoped_access_info)
        plugin.auth_url = settings.OPENSTACK_KEYSTONE_URL
        client = self.ks_client_module.Client(
            session=mox.IsA(session.Session), auth=plugin)

        plugin = v3_auth.Token(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            token=self.data.unscoped_access_info.auth_token,
            domain_name=DEFAULT_DOMAIN,
            reauthenticate=False)
        plugin.get_access(mox.IsA(session.Session)).AndReturn(
            self.data.domain_scoped_access_info)

        client.projects = self.mox.CreateMockAnything()
        client.projects.list(user=user.id).AndReturn(projects)

        plugin = v3_auth.Token(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            token=self.data.unscoped_access_info.auth_token,
            project_id=self.data.project_one.id,
            reauthenticate=False)
        self.scoped_token_auth = plugin
        plugin.get_access(mox.IsA(session.Session)).AndReturn(
            self.data.unscoped_access_info)
        self.ks_client_module.Client(
            session=mox.IsA(session.Session),
            auth=plugin)

        # mock switch
        plugin = v3_auth.Token(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            token=self.data.unscoped_access_info.auth_token,
            project_id=None,
            reauthenticate=False)
        plugin.get_access(mox.IsA(session.Session)).AndReturn(
            self.data.unscoped_access_info)

        plugin.auth_url = settings.OPENSTACK_KEYSTONE_URL
        client = self.ks_client_module.Client(session=mox.IsA(session.Session),
                                              auth=plugin)

        client.projects = self.mox.CreateMockAnything()
        client.projects.list(user=user.id).AndReturn(projects)

        plugin = v3_auth.Token(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            token=self.data.unscoped_access_info.auth_token,
            project_id=self.data.project_one.id,
            reauthenticate=False)
        plugin.get_access(mox.IsA(session.Session)).AndReturn(
            settings.OPENSTACK_KEYSTONE_URL)

        plugin.get_sp_auth_url(
            mox.IsA(session.Session), target_provider
        ).AndReturn('https://k2kserviceprovider/sp_url')
        plugin = v3_auth.Keystone2Keystone(base_plugin=plugin,
                                           service_provider=target_provider)
        plugin.get_access(mox.IsA(session.Session)). \
            AndReturn(self.sp_data.unscoped_access_info)
        plugin.auth_url = 'http://service_provider_endp/identity/v3'

        # mock authenticate for service provider
        sp_projects = [self.sp_data.project_one, self.sp_data.project_two]

        sp_unscoped_auth = v3_auth.Token(
            auth_url='http://service_provider_endp/identity/v3',
            token=self.sp_data.federated_unscoped_access_info.auth_token,
            project_id=None,
            reauthenticate=False)
        sp_unscoped_auth.get_access(mox.IsA(session.Session)).AndReturn(
            self.sp_data.federated_unscoped_access_info)
        sp_unscoped_auth.auth_url = settings.OPENSTACK_KEYSTONE_URL

        sp_unscoped_auth.auth_url = plugin.auth_url
        client = self.ks_client_module.Client(
            session=mox.IsA(session.Session),
            auth=sp_unscoped_auth)

        client.auth = self.mox.CreateMockAnything()
        client.auth.domains().AndReturn(domains)

        sp_unscoped_auth.auth_url = plugin.auth_url
        client = self.ks_client_module.Client(
            session=mox.IsA(session.Session),
            auth=sp_unscoped_auth)

        client.federation = self.mox.CreateMockAnything()
        client.federation.projects = self.mox.CreateMockAnything()
        client.federation.projects.list().AndReturn(sp_projects)

        plugin = v3_auth.Token(
            auth_url='http://service_provider_endp/identity/v3',
            token=self.sp_data.federated_unscoped_access_info.auth_token,
            project_id=self.sp_data.project_one.id,
            reauthenticate=False)
        self.scoped_token_auth = plugin
        plugin.get_access(mox.IsA(session.Session)).AndReturn(
            self.sp_data.federated_unscoped_access_info)
        self.ks_client_module.Client(
            session=mox.IsA(session.Session),
            auth=plugin)

        self.mox.ReplayAll()

        # Log in
        url = reverse('login')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        response = self.client.post(url, form_data)
        self.assertRedirects(response, settings.LOGIN_REDIRECT_URL)

        # Switch
        url = reverse('switch_keystone_provider', args=[target_provider])
        form_data['keystone_provider'] = target_provider
        response = self.client.get(url, form_data, follow=True)
        self.assertRedirects(response, settings.LOGIN_REDIRECT_URL)

        # Assert keystone provider has changed
        self.assertEqual(self.client.session['keystone_provider_id'],
                         target_provider)
        # These should not change
        self.assertEqual(self.client.session['k2k_base_unscoped_token'],
                         self.data.unscoped_access_info.auth_token)
        self.assertEqual(self.client.session['k2k_auth_url'],
                         settings.OPENSTACK_KEYSTONE_URL)

    def test_switch_keystone_provider_local(self):
        self.data = data_v3.generate_test_data(service_providers=True)
        keystone_provider = 'localkeystone'
        projects = [self.data.project_one, self.data.project_two]
        domains = []
        user = self.data.user
        form_data = self.get_form_data(user)

        # mock authenticate

        plugin = v3_auth.Password(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            password=self.data.user.password,
            username=self.data.user.name,
            user_domain_name=DEFAULT_DOMAIN,
            unscoped=True)
        plugin.get_access(mox.IsA(session.Session)). \
            AndReturn(self.data.unscoped_access_info)
        plugin.auth_url = settings.OPENSTACK_KEYSTONE_URL
        client = self.ks_client_module.Client(
            session=mox.IsA(session.Session), auth=plugin)

        plugin = v3_auth.Token(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            token=self.data.unscoped_access_info.auth_token,
            domain_name=DEFAULT_DOMAIN,
            reauthenticate=False)

        plugin.get_access(mox.IsA(session.Session)).AndReturn(
            self.data.domain_scoped_access_info)

        client.projects = self.mox.CreateMockAnything()
        client.projects.list(user=user.id).AndReturn(projects)

        plugin = v3_auth.Token(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            token=self.data.unscoped_access_info.auth_token,
            project_id=self.data.project_one.id,
            reauthenticate=False)

        plugin.get_access(mox.IsA(session.Session)).AndReturn(
            self.data.unscoped_access_info)
        self.ks_client_module.Client(
            session=mox.IsA(session.Session),
            auth=plugin)

        plugin = v3_auth.Token(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            token=self.data.unscoped_access_info.auth_token,
            project_id=None,
            reauthenticate=False)

        plugin.get_access(mox.IsA(session.Session)).AndReturn(
            self.data.unscoped_access_info)
        plugin.auth_url = settings.OPENSTACK_KEYSTONE_URL

        unscoped_auth = v3_auth.Token(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            token=self.data.unscoped_access_info.auth_token,
            project_id=None,
            reauthenticate=False)

        unscoped_auth.get_access(mox.IsA(session.Session)).AndReturn(
            self.data.unscoped_access_info)
        unscoped_auth.auth_url = settings.OPENSTACK_KEYSTONE_URL

        client = self.ks_client_module.Client(
            session=mox.IsA(session.Session),
            auth=unscoped_auth)

        client.auth = self.mox.CreateMockAnything()
        client.auth.domains().AndReturn(domains)

        client = self.ks_client_module.Client(
            session=mox.IsA(session.Session),
            auth=unscoped_auth)

        client.projects = self.mox.CreateMockAnything()
        client.projects.list(user=user.id).AndReturn(projects)

        plugin = v3_auth.Token(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            token=self.data.unscoped_access_info.auth_token,
            project_id=self.data.project_one.id,
            reauthenticate=False)
        self.scoped_token_auth = plugin
        plugin.get_access(mox.IsA(session.Session)).AndReturn(
            self.data.unscoped_access_info)
        self.ks_client_module.Client(
            session=mox.IsA(session.Session),
            auth=plugin)

        self.mox.ReplayAll()

        # Log in
        url = reverse('login')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        response = self.client.post(url, form_data)
        self.assertRedirects(response, settings.LOGIN_REDIRECT_URL)

        # Switch
        url = reverse('switch_keystone_provider', args=[keystone_provider])
        form_data['keystone_provider'] = keystone_provider
        response = self.client.get(url, form_data, follow=True)
        self.assertRedirects(response, settings.LOGIN_REDIRECT_URL)

        # Assert nothing has changed since we are going from local to local
        self.assertEqual(self.client.session['keystone_provider_id'],
                         keystone_provider)
        self.assertEqual(self.client.session['k2k_base_unscoped_token'],
                         self.data.unscoped_access_info.auth_token)
        self.assertEqual(self.client.session['k2k_auth_url'],
                         settings.OPENSTACK_KEYSTONE_URL)

    def test_switch_keystone_provider_local_fail(self):
        self.data = data_v3.generate_test_data(service_providers=True)
        keystone_provider = 'localkeystone'
        projects = [self.data.project_one, self.data.project_two]
        user = self.data.user
        form_data = self.get_form_data(user)

        # mock authenticate

        plugin = v3_auth.Password(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            password=self.data.user.password,
            username=self.data.user.name,
            user_domain_name=DEFAULT_DOMAIN,
            unscoped=True)
        plugin.get_access(mox.IsA(session.Session)). \
            AndReturn(self.data.unscoped_access_info)
        plugin.auth_url = settings.OPENSTACK_KEYSTONE_URL
        client = self.ks_client_module.Client(
            session=mox.IsA(session.Session), auth=plugin)

        plugin = v3_auth.Token(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            token=self.data.unscoped_access_info.auth_token,
            domain_name=DEFAULT_DOMAIN,
            reauthenticate=False)

        plugin.get_access(mox.IsA(session.Session)).AndReturn(
            self.data.domain_scoped_access_info)

        client.projects = self.mox.CreateMockAnything()
        client.projects.list(user=user.id).AndReturn(projects)

        plugin = v3_auth.Token(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            token=self.data.unscoped_access_info.auth_token,
            project_id=self.data.project_one.id,
            reauthenticate=False)
        self.scoped_token_auth = plugin
        plugin.get_access(mox.IsA(session.Session)).AndReturn(
            self.data.unscoped_access_info)
        self.ks_client_module.Client(
            session=mox.IsA(session.Session),
            auth=plugin)

        # Let using the base token for logging in fail
        plugin = v3_auth.Token(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            token=self.data.unscoped_access_info.auth_token,
            project_id=None,
            reauthenticate=False)
        plugin.get_access(mox.IsA(session.Session)).AndRaise(
            keystone_exceptions.AuthorizationFailure)
        plugin.auth_url = settings.OPENSTACK_KEYSTONE_URL
        self.mox.ReplayAll()

        # Log in
        url = reverse('login')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        response = self.client.post(url, form_data)
        self.assertRedirects(response, settings.LOGIN_REDIRECT_URL)

        # Switch
        url = reverse('switch_keystone_provider', args=[keystone_provider])
        form_data['keystone_provider'] = keystone_provider
        response = self.client.get(url, form_data, follow=True)
        self.assertRedirects(response, settings.LOGIN_REDIRECT_URL)

        # Assert
        self.assertEqual(self.client.session['keystone_provider_id'],
                         keystone_provider)
        self.assertEqual(self.client.session['k2k_base_unscoped_token'],
                         self.data.unscoped_access_info.auth_token)
        self.assertEqual(self.client.session['k2k_auth_url'],
                         settings.OPENSTACK_KEYSTONE_URL)


class OpenStackAuthTestsWebSSO(OpenStackAuthTestsMixin,
                               test.TestCase):

    def setUp(self):
        super(OpenStackAuthTestsWebSSO, self).setUp()

        self.mox = mox.Mox()
        self.addCleanup(self.mox.VerifyAll)
        self.addCleanup(self.mox.UnsetStubs)

        self.data = data_v3.generate_test_data()
        self.ks_client_module = client_v3

        self.idp_id = uuid.uuid4().hex
        self.idp_oidc_id = uuid.uuid4().hex
        self.idp_saml2_id = uuid.uuid4().hex

        settings.OPENSTACK_API_VERSIONS['identity'] = 3
        settings.OPENSTACK_KEYSTONE_URL = 'http://localhost/identity/v3'
        settings.WEBSSO_ENABLED = True
        settings.WEBSSO_CHOICES = (
            ('credentials', 'Keystone Credentials'),
            ('oidc', 'OpenID Connect'),
            ('saml2', 'Security Assertion Markup Language'),
            (self.idp_oidc_id, 'IDP OIDC'),
            (self.idp_saml2_id, 'IDP SAML2')
        )
        settings.WEBSSO_IDP_MAPPING = {
            self.idp_oidc_id: (self.idp_id, 'oidc'),
            self.idp_saml2_id: (self.idp_id, 'saml2')
        }

        self.mox.StubOutClassWithMocks(token_endpoint, 'Token')
        self.mox.StubOutClassWithMocks(v3_auth, 'Token')
        self.mox.StubOutClassWithMocks(v3_auth, 'Password')
        self.mox.StubOutClassWithMocks(client_v3, 'Client')

    def test_login_form(self):
        url = reverse('login')

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'credentials')
        self.assertContains(response, 'oidc')
        self.assertContains(response, 'saml2')
        self.assertContains(response, self.idp_oidc_id)
        self.assertContains(response, self.idp_saml2_id)

    def test_websso_redirect_by_protocol(self):
        origin = 'http://testserver/auth/websso/'
        protocol = 'oidc'
        redirect_url = ('%s/auth/OS-FEDERATION/websso/%s?origin=%s' %
                        (settings.OPENSTACK_KEYSTONE_URL, protocol, origin))

        form_data = {'auth_type': protocol,
                     'region': 'default'}
        url = reverse('login')

        # POST to the page and redirect to keystone.
        response = self.client.post(url, form_data)
        self.assertRedirects(response, redirect_url, status_code=302,
                             target_status_code=404)

    def test_websso_redirect_by_idp(self):
        origin = 'http://testserver/auth/websso/'
        protocol = 'oidc'
        redirect_url = ('%s/auth/OS-FEDERATION/identity_providers/%s'
                        '/protocols/%s/websso?origin=%s' %
                        (settings.OPENSTACK_KEYSTONE_URL, self.idp_id,
                         protocol, origin))

        form_data = {'auth_type': self.idp_oidc_id,
                     'region': 'default'}
        url = reverse('login')

        # POST to the page and redirect to keystone.
        response = self.client.post(url, form_data)
        self.assertRedirects(response, redirect_url, status_code=302,
                             target_status_code=404)

    @override_settings(WEBSSO_KEYSTONE_URL='http://keystone-public/identity/v3')
    def test_websso_redirect_using_websso_keystone_url(self):
        origin = 'http://testserver/auth/websso/'
        protocol = 'oidc'
        redirect_url = ('%s/auth/OS-FEDERATION/identity_providers/%s'
                        '/protocols/%s/websso?origin=%s' %
                        (settings.WEBSSO_KEYSTONE_URL, self.idp_id,
                         protocol, origin))

        form_data = {'auth_type': self.idp_oidc_id,
                     'region': 'default'}
        url = reverse('login')

        # POST to the page and redirect to keystone.
        response = self.client.post(url, form_data)
        # verify that the request was sent back to WEBSSO_KEYSTONE_URL
        self.assertRedirects(response, redirect_url, status_code=302,
                             target_status_code=404)

    def test_websso_login(self):
        projects = [self.data.project_one, self.data.project_two]
        domains = []
        form_data = {
            'token': self.data.federated_unscoped_access_info.auth_token
        }

        unscoped_auth = v3_auth.Token(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            token=self.data.federated_unscoped_access_info.auth_token,
            project_id=None,
            reauthenticate=False)

        unscoped_auth.get_access(mox.IsA(session.Session)).AndReturn(
            self.data.federated_unscoped_access_info)
        unscoped_auth.auth_url = settings.OPENSTACK_KEYSTONE_URL

        unscoped_auth.auth_url = settings.OPENSTACK_KEYSTONE_URL
        client = self.ks_client_module.Client(
            session=mox.IsA(session.Session),
            auth=unscoped_auth)

        client.auth = self.mox.CreateMockAnything()
        client.auth.domains().AndReturn(domains)

        unscoped_auth.auth_url = settings.OPENSTACK_KEYSTONE_URL
        client = self.ks_client_module.Client(
            session=mox.IsA(session.Session),
            auth=unscoped_auth)

        client.federation = self.mox.CreateMockAnything()
        client.federation.projects = self.mox.CreateMockAnything()
        client.federation.projects.list().AndReturn(projects)

        plugin = v3_auth.Token(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            token=self.data.unscoped_access_info.auth_token,
            project_id=self.data.project_one.id,
            reauthenticate=False)
        self.scoped_token_auth = plugin
        plugin.get_access(mox.IsA(session.Session)).AndReturn(
            self.data.unscoped_access_info)
        self.ks_client_module.Client(
            session=mox.IsA(session.Session),
            auth=plugin)

        self.mox.ReplayAll()

        url = reverse('websso')

        # POST to the page to log in.
        response = self.client.post(url, form_data)
        self.assertRedirects(response, settings.LOGIN_REDIRECT_URL)

    def test_websso_login_with_auth_in_url(self):
        settings.OPENSTACK_KEYSTONE_URL =\
            'http://auth.openstack.org/identity/v3'

        projects = [self.data.project_one, self.data.project_two]
        domains = []
        form_data = {'token': self.data.unscoped_access_info.auth_token}

        unscoped_auth = v3_auth.Token(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            token=self.data.federated_unscoped_access_info.auth_token,
            project_id=None,
            reauthenticate=False)
        unscoped_auth.get_access(mox.IsA(session.Session)).AndReturn(
            self.data.federated_unscoped_access_info)
        unscoped_auth.auth_url = settings.OPENSTACK_KEYSTONE_URL

        unscoped_auth.auth_url = settings.OPENSTACK_KEYSTONE_URL
        client = self.ks_client_module.Client(
            session=mox.IsA(session.Session),
            auth=unscoped_auth)

        client.auth = self.mox.CreateMockAnything()
        client.auth.domains().AndReturn(domains)

        unscoped_auth.auth_url = settings.OPENSTACK_KEYSTONE_URL
        client = self.ks_client_module.Client(
            session=mox.IsA(session.Session),
            auth=unscoped_auth)

        client.federation = self.mox.CreateMockAnything()
        client.federation.projects = self.mox.CreateMockAnything()
        client.federation.projects.list().AndReturn(projects)

        plugin = v3_auth.Token(
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            token=self.data.unscoped_access_info.auth_token,
            project_id=self.data.project_one.id,
            reauthenticate=False)
        self.scoped_token_auth = plugin
        plugin.get_access(mox.IsA(session.Session)).AndReturn(
            self.data.unscoped_access_info)
        self.ks_client_module.Client(
            session=mox.IsA(session.Session),
            auth=plugin)

        self.mox.ReplayAll()

        url = reverse('websso')

        # POST to the page to log in.
        response = self.client.post(url, form_data)
        self.assertRedirects(response, settings.LOGIN_REDIRECT_URL)

    def test_websso_login_default_redirect(self):
        origin = 'http://testserver/auth/websso/'
        protocol = 'oidc'
        redirect_url = ('%s/auth/OS-FEDERATION/websso/%s?origin=%s' %
                        (settings.OPENSTACK_KEYSTONE_URL, protocol, origin))

        settings.WEBSSO_DEFAULT_REDIRECT = True
        settings.WEBSSO_DEFAULT_REDIRECT_PROTOCOL = 'oidc'
        settings.WEBSSO_DEFAULT_REDIRECT_REGION = (
            settings.OPENSTACK_KEYSTONE_URL)

        url = reverse('login')

        # POST to the page and redirect to keystone.
        response = self.client.get(url)
        self.assertRedirects(response, redirect_url, status_code=302,
                             target_status_code=404)

    def test_websso_logout_default_redirect(self):
        settings.WEBSSO_DEFAULT_REDIRECT = True
        settings.WEBSSO_DEFAULT_REDIRECT_LOGOUT = 'http://idptest/logout'

        url = reverse('logout')

        # POST to the page and redirect to logout method from idp.
        response = self.client.get(url)
        self.assertRedirects(response, settings.WEBSSO_DEFAULT_REDIRECT_LOGOUT,
                             status_code=302, target_status_code=301)


class OpenStackAuthTestsV3WithMock(test.TestCase):

    def get_form_data(self, user):
        return {'region': "default",
                'domain': DEFAULT_DOMAIN,
                'password': user.password,
                'username': user.name}

    def setUp(self):
        super(OpenStackAuthTestsV3WithMock, self).setUp()

        if getattr(self, 'interface', None):
            override = self.settings(OPENSTACK_ENDPOINT_TYPE=self.interface)
            override.enable()
            self.addCleanup(override.disable)

        self.data = data_v3.generate_test_data()
        settings.OPENSTACK_API_VERSIONS['identity'] = 3
        settings.OPENSTACK_KEYSTONE_URL = "http://localhost:5000/v3"

    @mock.patch('keystoneauth1.identity.v3.Token.get_access')
    @mock.patch('keystoneauth1.identity.v3.Password.get_access')
    @mock.patch('keystoneclient.v3.client.Client')
    def test_login(self, mock_client, mock_get_access, mock_get_access_token):
        projects = [self.data.project_one, self.data.project_two]
        user = self.data.user
        form_data = self.get_form_data(user)
        url = reverse('login')

        mock_get_access.return_value = self.data.unscoped_access_info
        mock_client.return_value.projects.list.return_value = projects
        # TODO(stephenfin): What is the return type of this method?
        mock_get_access_token.return_value = self.data.unscoped_access_info

        # GET the page to set the test cookie.
        response = self.client.get(url, form_data)
        self.assertEqual(response.status_code, 200)

        # POST to the page to log in.
        response = self.client.post(url, form_data)
        self.assertRedirects(response, settings.LOGIN_REDIRECT_URL)

    @mock.patch('keystoneauth1.identity.v3.Password.get_access')
    def test_invalid_credentials(self, mock_get_access):
        user = self.data.user
        form_data = self.get_form_data(user)
        form_data['password'] = "invalid"
        url = reverse('login')

        mock_get_access.side_effect = keystone_exceptions.Unauthorized(401)

        # GET the page to set the test cookie.
        response = self.client.get(url, form_data)
        self.assertEqual(response.status_code, 200)

        # POST to the page to log in.
        response = self.client.post(url, form_data)
        self.assertTemplateUsed(response, 'auth/login.html')
        self.assertContains(response, "Invalid credentials.")

        mock_get_access.assert_called_once_with(IsA(session.Session))

    @mock.patch('keystoneauth1.identity.v3.Password.get_access')
    def test_exception(self, mock_get_access):
        user = self.data.user
        form_data = self.get_form_data(user)
        url = reverse('login')

        mock_get_access.side_effect = keystone_exceptions.ClientException(500)

        # GET the page to set the test cookie.
        response = self.client.get(url, form_data)
        self.assertEqual(response.status_code, 200)

        # POST to the page to log in.
        response = self.client.post(url, form_data)
        self.assertTemplateUsed(response, 'auth/login.html')
        self.assertContains(response,
                            ("An error occurred authenticating. Please try "
                             "again later."))

        mock_get_access.assert_called_once_with(IsA(session.Session))

    @mock.patch('keystoneauth1.identity.v3.Password.get_access')
    def test_password_expired(self, mock_get_access):
        user = self.data.user
        form_data = self.get_form_data(user)
        url = reverse('login')

        class ExpiredException(keystone_exceptions.Unauthorized):
            http_status = 401
            message = ("The password is expired and needs to be changed"
                       " for user: %s." % user.id)

        mock_get_access.side_effect = ExpiredException()

        # GET the page to set the test cookie.
        response = self.client.get(url, form_data)
        self.assertEqual(response.status_code, 200)

        # POST to the page to log in.
        response = self.client.post(url, form_data)

        # This fails with TemplateDoesNotExist for some reason.
        # self.assertRedirects(response, reverse('password', args=[user.id]))
        # so instead we check for the redirect manually:
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/password/%s/" % user.id)

        mock_get_access.assert_called_once_with(IsA(session.Session))

    def test_login_form_multidomain(self):
        override = self.settings(OPENSTACK_KEYSTONE_MULTIDOMAIN_SUPPORT=True)
        override.enable()
        self.addCleanup(override.disable)

        url = reverse('login')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="id_domain"')
        self.assertContains(response, 'name="domain"')

    def test_login_form_multidomain_dropdown(self):
        override = self.settings(OPENSTACK_KEYSTONE_MULTIDOMAIN_SUPPORT=True,
                                 OPENSTACK_KEYSTONE_DOMAIN_DROPDOWN=True,
                                 OPENSTACK_KEYSTONE_DOMAIN_CHOICES=(
                                     ('Default', 'Default'),)
                                 )
        override.enable()
        self.addCleanup(override.disable)

        url = reverse('login')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="id_domain"')
        self.assertContains(response, 'name="domain"')
        self.assertContains(response, 'option value="Default"')
        settings.OPENSTACK_KEYSTONE_DOMAIN_DROPDOWN = False

    @mock.patch.object(projects.ProjectManager, 'list')
    def test_tenant_sorting(self, mock_project_list):
        projects = [self.data.project_two, self.data.project_one]
        expected_projects = [self.data.project_one, self.data.project_two]
        user = self.data.user

        mock_project_list.return_value = projects

        project_list = utils.get_project_list(
            user_id=user.id,
            auth_url=settings.OPENSTACK_KEYSTONE_URL,
            token=self.data.unscoped_access_info.auth_token)
        self.assertEqual(project_list, expected_projects)

        mock_project_list.assert_called_once()

    @mock.patch.object(v3_auth.Token, 'get_access')
    @mock.patch.object(password.PasswordPlugin, 'list_projects')
    @mock.patch.object(v3_auth.Password, 'get_access')
    def test_login_with_disabled_project(self, mock_get_access,
                                         mock_project_list,
                                         mock_get_access_token):
        # Test to validate that authentication will not try to get
        # scoped token for disabled project.
        projects = [self.data.project_two, self.data.project_one]
        user = self.data.user

        mock_get_access.return_value = self.data.unscoped_access_info
        mock_project_list.return_value = projects
        mock_get_access_token.return_value = self.data.unscoped_access_info

        form_data = self.get_form_data(user)

        url = reverse('login')

        # GET the page to set the test cookie.
        response = self.client.get(url, form_data)
        self.assertEqual(response.status_code, 200)

        # POST to the page to log in.
        response = self.client.post(url, form_data)
        self.assertRedirects(response, settings.LOGIN_REDIRECT_URL)
        mock_get_access.assert_called_once_with(IsA(session.Session))
        mock_get_access_token.assert_called_with(IsA(session.Session))
        mock_project_list.assert_called_once_with(
            IsA(session.Session),
            IsA(v3_auth.Password),
            self.data.unscoped_access_info)

    @mock.patch.object(v3_auth.Token, 'get_access')
    @mock.patch.object(password.PasswordPlugin, 'list_projects')
    @mock.patch.object(v3_auth.Password, 'get_access')
    def test_no_enabled_projects(self, mock_get_access, mock_project_list,
                                 mock_get_access_token):
        projects = [self.data.project_two]
        user = self.data.user

        mock_get_access.return_value = self.data.unscoped_access_info
        mock_project_list.return_value = projects
        mock_get_access_token.return_value = self.data.unscoped_access_info

        form_data = self.get_form_data(user)

        url = reverse('login')

        # GET the page to set the test cookie.
        response = self.client.get(url, form_data)
        self.assertEqual(response.status_code, 200)

        # POST to the page to log in.
        response = self.client.post(url, form_data)
        self.assertRedirects(response, settings.LOGIN_REDIRECT_URL)

        mock_get_access.assert_called_once_with(IsA(session.Session))
        mock_get_access_token.assert_called_with(IsA(session.Session))
        mock_project_list.assert_called_once_with(
            IsA(session.Session),
            IsA(v3_auth.Password),
            self.data.unscoped_access_info)

    @mock.patch.object(v3_auth.Token, 'get_access')
    @mock.patch.object(password.PasswordPlugin, 'list_projects')
    @mock.patch.object(v3_auth.Password, 'get_access')
    def test_no_projects(self, mock_get_access, mock_project_list,
                         mock_get_access_token):
        user = self.data.user
        form_data = self.get_form_data(user)

        mock_get_access.return_value = self.data.unscoped_access_info
        mock_get_access_token.return_value = self.data.unscoped_access_info
        mock_project_list.return_value = []

        url = reverse('login')

        # GET the page to set the test cookie.
        response = self.client.get(url, form_data)
        self.assertEqual(response.status_code, 200)

        # POST to the page to log in.
        response = self.client.post(url, form_data)
        self.assertRedirects(response, settings.LOGIN_REDIRECT_URL)

        mock_get_access.assert_called_once_with(IsA(session.Session))
        mock_get_access_token.assert_called_with(IsA(session.Session))
        mock_project_list.assert_called_once_with(
            IsA(session.Session),
            IsA(v3_auth.Password),
            self.data.unscoped_access_info)

    @mock.patch.object(v3_auth.Token, 'get_access')
    @mock.patch.object(projects.ProjectManager, 'list')
    @mock.patch.object(v3_auth.Password, 'get_access')
    def test_fail_projects(self, mock_get_access, mock_project_list,
                           mock_get_access_token):
        user = self.data.user

        form_data = self.get_form_data(user)

        mock_get_access.return_value = self.data.unscoped_access_info
        mock_get_access_token.return_value = self.data.unscoped_access_info
        mock_project_list.side_effect = keystone_exceptions.AuthorizationFailure

        url = reverse('login')

        # GET the page to set the test cookie.
        response = self.client.get(url, form_data)
        self.assertEqual(response.status_code, 200)

        # POST to the page to log in.
        response = self.client.post(url, form_data)
        self.assertTemplateUsed(response, 'auth/login.html')
        self.assertContains(response,
                            'Unable to retrieve authorized projects.')

        mock_get_access.assert_called_once_with(IsA(session.Session))
        mock_get_access_token.assert_called_with(IsA(session.Session))
        mock_project_list.assert_called_once_with(user=user.id)

    @mock.patch.object(v3_auth.Token, 'get_access')
    @mock.patch.object(password.PasswordPlugin, 'list_projects')
    @mock.patch.object(v3_auth.Password, 'get_access')
    def test_switch(self, mock_get_access, mock_project_list,
                    mock_get_access_token,
                    next=None):
        project = self.data.project_two
        projects = [self.data.project_one, self.data.project_two]
        user = self.data.user
        scoped = self.data.scoped_access_info

        form_data = self.get_form_data(user)

        mock_get_access.return_value = self.data.unscoped_access_info
        mock_get_access_token.return_value = scoped
        mock_project_list.return_value = projects

        url = reverse('login')

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        response = self.client.post(url, form_data)
        self.assertRedirects(response, settings.LOGIN_REDIRECT_URL)

        url = reverse('switch_tenants', args=[project.id])

        scoped._project['id'] = self.data.project_two.id

        if next:
            form_data.update({auth.REDIRECT_FIELD_NAME: next})

        response = self.client.get(url, form_data)

        if next:
            expected_url = next
            self.assertEqual(response['location'], expected_url)
        else:
            self.assertRedirects(response, settings.LOGIN_REDIRECT_URL)

        self.assertEqual(self.client.session['token'].project['id'],
                         scoped.project_id)

        mock_get_access.assert_called_once_with(IsA(session.Session))
        mock_get_access_token.assert_called_with(IsA(session.Session))
        mock_project_list.assert_called_once_with(
            IsA(session.Session),
            IsA(v3_auth.Password),
            self.data.unscoped_access_info)

    def test_switch_with_next(self):
        self.test_switch(next='/next_url')

    @mock.patch.object(v3_auth.Token, 'get_access')
    @mock.patch.object(password.PasswordPlugin, 'list_projects')
    @mock.patch.object(v3_auth.Password, 'get_access')
    def test_switch_region(self, mock_get_access, mock_project_list,
                           mock_get_access_token,
                           next=None):
        projects = [self.data.project_one, self.data.project_two]
        user = self.data.user
        scoped = self.data.unscoped_access_info
        sc = self.data.service_catalog

        form_data = self.get_form_data(user)

        mock_get_access.return_value = self.data.unscoped_access_info
        mock_get_access_token.return_value = scoped
        mock_project_list.return_value = projects

        url = reverse('login')

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        response = self.client.post(url, form_data)
        self.assertRedirects(response, settings.LOGIN_REDIRECT_URL)

        old_region = sc.get_endpoints()['compute'][0]['region']
        self.assertEqual(self.client.session['services_region'], old_region)

        region = sc.get_endpoints()['compute'][1]['region']
        url = reverse('switch_services_region', args=[region])

        form_data['region_name'] = region

        if next:
            form_data.update({auth.REDIRECT_FIELD_NAME: next})

        response = self.client.get(url, form_data)

        if next:
            expected_url = next
            self.assertEqual(response['location'], expected_url)
        else:
            self.assertRedirects(response, settings.LOGIN_REDIRECT_URL)

        self.assertEqual(self.client.session['services_region'], region)

        mock_get_access.assert_called_once_with(IsA(session.Session))
        mock_get_access_token.assert_called_with(IsA(session.Session))
        mock_project_list.assert_called_once_with(
            IsA(session.Session),
            IsA(v3_auth.Password),
            self.data.unscoped_access_info)

    def test_switch_region_with_next(self, next=None):
        self.test_switch_region(next='/next_url')
