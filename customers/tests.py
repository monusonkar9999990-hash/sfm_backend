"""End-to-end tests for the customer endpoints, and for registering a site
against one.

The site half lives here rather than in `sitevisits.tests` because what is
being tested is the join between the two modules — that a site cannot be
registered against a customer who does not exist, and that the name copied
onto it comes from the customers table rather than from the caller.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Role
from sitevisits.models import Site

from .models import Customer, CustomerType, normalise_mobile

User = get_user_model()

PASSWORD = 'Str0ng-Pass!7'


def payload(**overrides):
    body = {
        'name': 'Shree Balaji Traders',
        'contact_person': 'Ramesh Gupta',
        'phone': '9811122233',
        'email': 'balaji@example.com',
        'type': CustomerType.DISTRIBUTOR,
        'address': '14, Karol Bagh Market',
        'city': 'New Delhi',
        'state': 'Delhi',
        'pincode': '110005',
        'gstin': '07AABCU9603R1ZM',
        'credit_limit': '500000.00',
    }
    body.update(overrides)
    return body


class CustomerTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.role = Role.objects.create(name='Field Executive', code='field_exec')
        cls.role.group.permissions.set(
            Permission.objects.filter(
                content_type__app_label='accounts', codename='onboard_customers'
            )
        )
        cls.user = User.objects.create_user(
            'sfm-0002',
            'Alex Mercer',
            email='alex@corp.com',
            mobile='+919876543210',
            password=PASSWORD,
            status=User.Status.ACTIVE,
            role=cls.role,
        )
        # The same role withheld, to prove the permission class bites.
        cls.outsider = User.objects.create_user(
            'sfm-0004',
            'Vikram Rao',
            email='vikram@corp.com',
            mobile='+919876500055',
            password=PASSWORD,
            status=User.Status.ACTIVE,
        )

    def setUp(self):
        # Throttle counters live in the cache and outlive a test otherwise.
        cache.clear()
        self.client.force_authenticate(self.user)

    # `version` is a path kwarg, not a literal — the same helper every other
    # suite in this project uses.
    def url(self, name, **kwargs):
        return reverse(name, kwargs={'version': 'v1', **kwargs})

    @property
    def list_url(self):
        return self.url('customers:list')

    # ------------------------------------------------------------ registering

    def test_a_customer_can_be_registered(self):
        response = self.client.post(self.list_url, payload(), format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['name'], 'Shree Balaji Traders')
        self.assertEqual(response.data['type'], CustomerType.DISTRIBUTOR)
        self.assertTrue(response.data['code'].startswith('CUS-'))

        customer = Customer.objects.get(pk=response.data['id'])
        self.assertEqual(customer.created_by, self.user)

    def test_the_credit_limit_comes_back_as_a_number(self):
        """The client reads it with `as num?`, so a string would crash it."""
        response = self.client.post(self.list_url, payload(), format='json')

        self.assertIsInstance(response.data['credit_limit'], float)
        self.assertEqual(response.data['credit_limit'], 500000.0)

    def test_a_phone_is_stored_as_ten_digits_however_it_arrives(self):
        response = self.client.post(
            self.list_url, payload(phone='+91 98111 22233'), format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['phone'], '9811122233')

    def test_the_same_number_cannot_be_registered_twice(self):
        self.client.post(self.list_url, payload(), format='json')

        response = self.client.post(
            self.list_url,
            payload(name='Another Shop', city='Jaipur', gstin=''),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('phone', response.data)

    def test_the_same_shop_in_a_different_city_is_a_different_shop(self):
        self.client.post(self.list_url, payload(), format='json')

        response = self.client.post(
            self.list_url,
            payload(city='Jaipur', phone='9811122244', gstin=''),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_the_same_shop_in_the_same_city_is_refused(self):
        self.client.post(self.list_url, payload(), format='json')

        response = self.client.post(
            self.list_url,
            payload(phone='9811122244', gstin=''),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('name', response.data)

    def test_a_malformed_gstin_is_refused(self):
        response = self.client.post(
            self.list_url, payload(gstin='NOT-A-GSTIN'), format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('gstin', response.data)

    def test_a_gstin_cannot_be_claimed_twice(self):
        self.client.post(self.list_url, payload(), format='json')

        response = self.client.post(
            self.list_url,
            payload(name='Balaji Depot', phone='9811122244'),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('gstin', response.data)

    def test_customers_without_a_gstin_are_not_duplicates_of_each_other(self):
        """The blank case is why uniqueness is not a database constraint."""
        first = self.client.post(self.list_url, payload(gstin=''), format='json')
        second = self.client.post(
            self.list_url,
            payload(name='Verma Hardware', phone='9822233344', gstin=''),
            format='json',
        )

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)

    def test_a_bad_pincode_is_refused(self):
        response = self.client.post(
            self.list_url, payload(pincode='0110A'), format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('pincode', response.data)

    def test_onboarding_needs_the_permission(self):
        self.client.force_authenticate(self.outsider)

        response = self.client.post(self.list_url, payload(), format='json')

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_reading_the_list_does_not(self):
        """An executive picking a customer for an order is not onboarding."""
        self.client.force_authenticate(self.outsider)

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_the_list_needs_a_token(self):
        self.client.force_authenticate(None)

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    # ---------------------------------------------------------------- reading

    def test_the_list_can_be_searched_and_filtered(self):
        self.client.post(self.list_url, payload(), format='json')
        self.client.post(
            self.list_url,
            payload(
                name='Verma Hardware',
                phone='9822233344',
                city='Noida',
                type=CustomerType.RETAILER,
                gstin='',
            ),
            format='json',
        )

        by_name = self.client.get(self.list_url, {'search': 'verma'})
        by_type = self.client.get(self.list_url, {'type': CustomerType.RETAILER})
        by_city = self.client.get(self.list_url, {'search': 'Delhi'})

        self.assertEqual(len(by_name.data['results']), 1)
        self.assertEqual(len(by_type.data['results']), 1)
        self.assertEqual(len(by_city.data['results']), 1)
        self.assertEqual(by_name.data['results'][0]['name'], 'Verma Hardware')

    def test_a_deactivated_customer_drops_off_the_list(self):
        created = self.client.post(self.list_url, payload(), format='json')
        Customer.objects.filter(pk=created.data['id']).update(is_active=False)

        listed = self.client.get(self.list_url)
        detail = self.client.get(
            self.url('customers:detail', pk=created.data['id'])
        )

        self.assertEqual(listed.data['count'], 0)
        self.assertEqual(detail.status_code, status.HTTP_404_NOT_FOUND)

    # ----------------------------------------------------- sites they own

    def register_customer(self):
        response = self.client.post(self.list_url, payload(), format='json')
        return response.data['id']

    def site_payload(self, customer_id, **overrides):
        body = {
            'name': 'Emerald Heights',
            'customer_id': customer_id,
            'stage': 'structure',
            'address': 'Plot 44, Sector 62',
            'city': 'Noida',
            'pincode': '201301',
            'estimated_value': '1250000.00',
        }
        body.update(overrides)
        return body

    def test_a_site_can_be_registered_against_a_customer(self):
        customer_id = self.register_customer()

        response = self.client.post(
            self.url('sitevisits:site-list'),
            self.site_payload(customer_id),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['customer_id'], customer_id)
        # The name is copied from the customers table, not from the caller.
        self.assertEqual(response.data['customer_name'], 'Shree Balaji Traders')
        self.assertTrue(response.data['code'].startswith('SITE-'))

    def test_a_site_needs_a_customer_that_exists(self):
        response = self.client.post(
            self.url('sitevisits:site-list'),
            self.site_payload('019fd0f4-0000-0000-0000-000000000000'),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('customer_id', response.data)

    def test_one_customer_cannot_have_two_sites_with_the_same_name(self):
        customer_id = self.register_customer()
        self.client.post(
            self.url('sitevisits:site-list'),
            self.site_payload(customer_id),
            format='json',
        )

        response = self.client.post(
            self.url('sitevisits:site-list'),
            self.site_payload(customer_id, city='Ghaziabad'),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('name', response.data)

    def test_half_a_fix_is_refused(self):
        customer_id = self.register_customer()

        response = self.client.post(
            self.url('sitevisits:site-list'),
            self.site_payload(customer_id, latitude='28.612900'),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_plotted_site_comes_back_with_its_location(self):
        customer_id = self.register_customer()

        response = self.client.post(
            self.url('sitevisits:site-list'),
            self.site_payload(
                customer_id, latitude='28.612900', longitude='77.229500'
            ),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertAlmostEqual(response.data['location']['lat'], 28.6129, places=4)

    def test_sites_can_be_narrowed_to_one_customer(self):
        first = self.register_customer()
        second = self.client.post(
            self.list_url,
            payload(name='Verma Hardware', phone='9822233344', gstin=''),
            format='json',
        ).data['id']

        self.client.post(
            self.url('sitevisits:site-list'),
            self.site_payload(first),
            format='json',
        )
        self.client.post(
            self.url('sitevisits:site-list'),
            self.site_payload(second, name='Verma Villas'),
            format='json',
        )

        response = self.client.get(
            self.url('sitevisits:site-list'), {'customer_id': second}
        )

        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['name'], 'Verma Villas')

    def test_registering_a_site_needs_the_permission(self):
        customer_id = self.register_customer()
        self.client.force_authenticate(self.outsider)

        response = self.client.post(
            self.url('sitevisits:site-list'),
            self.site_payload(customer_id),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_customer_ref_is_the_id_the_client_reads(self):
        """`Site.customer_ref` is what becomes a foreign key later, and the
        payload already calls it `customer_id` — so the two must agree."""
        customer_id = self.register_customer()

        self.client.post(
            self.url('sitevisits:site-list'),
            self.site_payload(customer_id),
            format='json',
        )

        site = Site.objects.get(name='Emerald Heights')
        self.assertEqual(site.customer_ref, customer_id)
        self.assertTrue(Customer.objects.filter(pk=site.customer_ref).exists())


class MobileNormalisationTests(APITestCase):
    def test_every_shape_a_visiting_card_uses(self):
        for raw in ['9811122233', '+919811122233', '09811122233', '98111 22233']:
            with self.subTest(raw=raw):
                self.assertEqual(normalise_mobile(raw), '9811122233')

    def test_a_number_that_is_not_one_is_left_alone_for_the_validator(self):
        self.assertEqual(normalise_mobile('12345'), '12345')


class CustomerModelTests(APITestCase):
    def test_codes_do_not_collide_for_rows_created_together(self):
        """Regression: the first version derived the code from the head of the
        UUIDv7 primary key, which is a millisecond timestamp — every customer
        registered in the same window got the same code."""
        codes = {
            Customer.objects.create(
                name=f'Shop {i}',
                contact_person='Someone',
                phone=f'98111222{i:02d}',
                city='Noida',
                state='Uttar Pradesh',
                pincode='201301',
            ).code
            for i in range(25)
        }

        self.assertEqual(len(codes), 25)

    def test_a_code_is_generated_when_none_is_given(self):
        customer = Customer.objects.create(
            name='Verma Hardware',
            contact_person='Sunil Verma',
            phone='9822233344',
            city='Noida',
            state='Uttar Pradesh',
            pincode='201301',
        )

        self.assertTrue(customer.code.startswith('CUS-'))
        self.assertEqual(len(customer.code), 12)

    def test_a_credit_limit_survives_the_round_trip_exactly(self):
        customer = Customer.objects.create(
            name='Shree Balaji Traders',
            contact_person='Ramesh Gupta',
            phone='9811122233',
            city='New Delhi',
            state='Delhi',
            pincode='110005',
            credit_limit=Decimal('123456.78'),
        )
        customer.refresh_from_db()

        self.assertEqual(customer.credit_limit, Decimal('123456.78'))


class CustomerEditTests(APITestCase):
    """Correcting a customer already on the books.

    Added for offline sync: an edit made on a device with no signal is queued
    as the one field that changed and replayed later, so the endpoint has to
    take a partial write — and has to not trip over the customer's own phone
    number when checking that the number is free.
    """

    @classmethod
    def setUpTestData(cls):
        cls.role = Role.objects.create(name='Field Executive', code='field_exec')
        cls.role.group.permissions.set(
            Permission.objects.filter(
                content_type__app_label='accounts', codename='onboard_customers'
            )
        )
        cls.user = User.objects.create_user(
            'sfm-0002', 'Alex Mercer', email='alex@corp.com',
            mobile='+919876543210', password=PASSWORD,
            status=User.Status.ACTIVE, role=cls.role,
        )
        cls.outsider = User.objects.create_user(
            'sfm-0004', 'Vikram Rao', email='vikram@corp.com',
            mobile='+919876500055', password=PASSWORD,
            status=User.Status.ACTIVE,
        )

    def setUp(self):
        cache.clear()
        self.client.force_authenticate(self.user)
        self.customer = Customer.objects.create(
            name='Shree Balaji Traders', contact_person='Ramesh Gupta',
            phone='9811122233', type=CustomerType.DISTRIBUTOR,
            city='New Delhi', state='Delhi', pincode='110005',
        )

    def url(self, pk=None):
        return reverse(
            'customers:detail', kwargs={'version': 'v1', 'pk': pk or self.customer.pk}
        )

    def test_one_field_can_be_corrected_without_sending_the_rest(self):
        response = self.client.patch(
            self.url(), {'phone': '9800000000'}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.phone, '9800000000')
        self.assertEqual(self.customer.contact_person, 'Ramesh Gupta')

    def test_saving_an_unchanged_number_is_not_a_duplicate_of_itself(self):
        response = self.client.patch(
            self.url(),
            {'phone': '9811122233', 'city': 'New Delhi'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_another_customers_number_is_still_refused(self):
        Customer.objects.create(
            name='Verma Hardware', contact_person='Sunil Verma',
            phone='9822233344', type=CustomerType.RETAILER,
            city='Noida', state='Uttar Pradesh', pincode='201301',
        )

        response = self.client.patch(
            self.url(), {'phone': '9822233344'}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('phone', response.data)

    def test_a_malformed_number_is_refused(self):
        response = self.client.patch(
            self.url(), {'phone': '12345'}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('phone', response.data)

    def test_editing_needs_the_onboarding_permission(self):
        self.client.force_authenticate(self.outsider)

        response = self.client.patch(
            self.url(), {'phone': '9800000000'}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.phone, '9811122233')

    def test_reading_one_still_needs_nothing_but_a_session(self):
        # A field executive picking a customer for an order must still be able
        # to read one without the onboarding permission.
        self.client.force_authenticate(self.outsider)

        response = self.client.get(self.url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_deactivated_customer_cannot_be_edited(self):
        self.customer.is_active = False
        self.customer.save(update_fields=['is_active'])

        response = self.client.patch(
            self.url(), {'phone': '9800000000'}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
