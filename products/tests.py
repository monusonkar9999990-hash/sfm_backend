"""End-to-end tests for the product endpoints.

Run against a throwaway `test_sfm_db`, like every other suite in this project.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Role

from .models import Product, ProductCategory, ProductUnit, StockStatus

User = get_user_model()

PASSWORD = 'Str0ng-Pass!7'


def payload(**overrides):
    body = {
        'name': 'OPC 53 Grade Cement',
        'category': ProductCategory.CEMENT,
        'brand': 'UltraTech',
        'description': '50 kg bag, IS 12269',
        'unit': ProductUnit.BAG,
        'mrp': '430.00',
        'selling_price': '400.00',
        'gst_percent': '28.00',
        'stock_quantity': 500,
    }
    body.update(overrides)
    return body


class ProductTestCase(APITestCase):
    """Shared fixtures: one user who may edit the catalogue, one who may not."""

    @classmethod
    def setUpTestData(cls):
        cls.role = Role.objects.create(name='Administrator', code='administrator')
        cls.role.group.permissions.set(
            Permission.objects.filter(
                content_type__app_label='accounts', codename='edit_master_data'
            )
        )
        cls.admin = User.objects.create_user(
            'sfm-0000',
            'Priya Nair',
            email='priya@corp.com',
            mobile='+919876500011',
            password=PASSWORD,
            status=User.Status.ACTIVE,
            role=cls.role,
        )
        # No role: reads the catalogue, cannot change it.
        cls.executive = User.objects.create_user(
            'sfm-0002',
            'Alex Mercer',
            email='alex@corp.com',
            mobile='+919876543210',
            password=PASSWORD,
            status=User.Status.ACTIVE,
        )

    def setUp(self):
        cache.clear()
        self.client.force_authenticate(self.admin)

    def url(self, name, **kwargs):
        return reverse(name, kwargs={'version': 'v1', **kwargs})

    @property
    def list_url(self):
        return self.url('products:list')

    def detail_url(self, pk):
        return self.url('products:detail', pk=pk)

    def make(self, **overrides):
        """A product straight into the table, bypassing the API."""
        body = {
            'name': 'OPC 53 Grade Cement',
            'category': ProductCategory.CEMENT,
            'brand': 'UltraTech',
            'unit': ProductUnit.BAG,
            'mrp': Decimal('430.00'),
            'selling_price': Decimal('400.00'),
            'gst_percent': Decimal('28.00'),
            'stock_quantity': 500,
        }
        body.update(overrides)
        return Product.objects.create(**body)


class CreateProductTests(ProductTestCase):
    def test_a_product_can_be_created(self):
        response = self.client.post(self.list_url, payload(), format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['name'], 'OPC 53 Grade Cement')
        self.assertEqual(response.data['category'], ProductCategory.CEMENT)
        self.assertEqual(Product.objects.count(), 1)

    def test_a_product_code_is_generated_when_none_is_given(self):
        response = self.client.post(self.list_url, payload(), format='json')

        code = response.data['product_code']
        self.assertTrue(code.startswith('PRD-'))
        self.assertEqual(len(code), 12)

    def test_a_supplied_product_code_is_kept_and_upper_cased(self):
        response = self.client.post(
            self.list_url, payload(product_code='cem-opc-53'), format='json'
        )

        self.assertEqual(response.data['product_code'], 'CEM-OPC-53')

    def test_codes_do_not_collide_across_products_created_together(self):
        """Regression guard: a code derived from the head of a UUIDv7 key is a
        millisecond timestamp, so a batch created together would share one."""
        codes = set()
        for i in range(25):
            response = self.client.post(
                self.list_url, payload(name=f'Product {i}'), format='json'
            )
            codes.add(response.data['product_code'])

        self.assertEqual(len(codes), 25)

    def test_money_comes_back_as_numbers(self):
        """The client reads these with `as num?` — a string would crash it."""
        response = self.client.post(self.list_url, payload(), format='json')

        self.assertIsInstance(response.data['mrp'], float)
        self.assertIsInstance(response.data['selling_price'], float)
        self.assertIsInstance(response.data['gst_percent'], float)
        self.assertEqual(response.data['selling_price'], 400.0)

    def test_the_payload_carries_the_keys_the_client_already_reads(self):
        response = self.client.post(self.list_url, payload(), format='json')

        self.assertEqual(response.data['sku'], response.data['product_code'])
        self.assertEqual(response.data['gst_rate'], response.data['gst_percent'])
        self.assertEqual(response.data['is_active'], response.data['active'])

    def test_stock_status_is_derived_from_the_quantity(self):
        cases = [
            (0, StockStatus.OUT_OF_STOCK),
            (10, StockStatus.LOW_STOCK),
            (500, StockStatus.IN_STOCK),
        ]
        for quantity, expected in cases:
            with self.subTest(quantity=quantity):
                response = self.client.post(
                    self.list_url,
                    payload(name=f'Product {quantity}', stock_quantity=quantity),
                    format='json',
                )
                self.assertEqual(response.data['stock'], expected)

    @override_settings(PRODUCTS_LOW_STOCK_THRESHOLD=25)
    def test_the_low_stock_line_is_a_setting_not_a_constant(self):
        product = self.make(stock_quantity=25)
        self.assertEqual(product.stock_status, StockStatus.LOW_STOCK)

    def test_the_discount_against_mrp_is_computed(self):
        response = self.client.post(self.list_url, payload(), format='json')

        # 430 -> 400 is a shade under 7%.
        self.assertAlmostEqual(response.data['discount_percent'], 6.98, places=2)


class ValidationTests(ProductTestCase):
    def test_selling_price_above_mrp_is_refused(self):
        response = self.client.post(
            self.list_url,
            payload(mrp='400.00', selling_price='430.00'),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('selling_price', response.data)

    def test_selling_price_equal_to_mrp_is_allowed(self):
        response = self.client.post(
            self.list_url,
            payload(mrp='430.00', selling_price='430.00'),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_a_negative_mrp_is_refused(self):
        response = self.client.post(
            self.list_url, payload(mrp='-1.00'), format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('mrp', response.data)

    def test_a_negative_selling_price_is_refused(self):
        response = self.client.post(
            self.list_url, payload(selling_price='-1.00'), format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('selling_price', response.data)

    def test_a_negative_gst_is_refused(self):
        response = self.client.post(
            self.list_url, payload(gst_percent='-5.00'), format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('gst_percent', response.data)

    def test_gst_above_a_hundred_percent_is_refused(self):
        response = self.client.post(
            self.list_url, payload(gst_percent='101.00'), format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('gst_percent', response.data)

    def test_a_duplicate_product_code_is_refused(self):
        self.client.post(
            self.list_url, payload(product_code='CEM-001'), format='json'
        )

        response = self.client.post(
            self.list_url,
            payload(name='Another Cement', product_code='CEM-001'),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('product_code', response.data)

    def test_a_duplicate_code_is_refused_by_the_database_too(self):
        """The serializer check is a courtesy; this is the guarantee."""
        self.make(product_code='CEM-001')

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make(name='Another', product_code='CEM-001')

    def test_the_database_refuses_a_price_above_mrp_written_around_the_api(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make(mrp=Decimal('100.00'), selling_price=Decimal('150.00'))

    def test_a_missing_name_is_refused(self):
        body = payload()
        del body['name']

        response = self.client.post(self.list_url, body, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('name', response.data)

    def test_an_unknown_category_is_refused(self):
        response = self.client.post(
            self.list_url, payload(category='rocket_fuel'), format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('category', response.data)


class ReadTests(ProductTestCase):
    def setUp(self):
        super().setUp()
        self.cement = self.make(
            name='OPC 53 Grade Cement',
            brand='UltraTech',
            product_code='CEM-001',
            category=ProductCategory.CEMENT,
            selling_price=Decimal('400.00'),
            mrp=Decimal('430.00'),
        )
        self.steel = self.make(
            name='TMT Bar 12mm',
            brand='Tata Tiscon',
            product_code='STL-001',
            category=ProductCategory.STEEL,
            unit=ProductUnit.TONNE,
            mrp=Decimal('62000.00'),
            selling_price=Decimal('58000.00'),
            stock_quantity=12,
        )
        self.paint = self.make(
            name='Wall Primer',
            brand='Asian Paints',
            product_code='PNT-001',
            category=ProductCategory.PAINT,
            unit=ProductUnit.LITRE,
            mrp=Decimal('310.00'),
            selling_price=Decimal('295.00'),
            active=False,
        )

    def test_the_list_comes_back_paginated(self):
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 3)
        self.assertIn('results', response.data)
        self.assertIn('next', response.data)

    def test_a_page_size_can_be_asked_for(self):
        response = self.client.get(self.list_url, {'page_size': 2})

        self.assertEqual(len(response.data['results']), 2)
        self.assertIsNotNone(response.data['next'])

    def test_the_page_size_is_capped(self):
        response = self.client.get(self.list_url, {'page_size': 5000})

        # Capped at 100, so three products still arrive in one page.
        self.assertEqual(len(response.data['results']), 3)

    def test_a_second_page_can_be_fetched(self):
        first = self.client.get(self.list_url, {'page_size': 2})
        second = self.client.get(self.list_url, {'page_size': 2, 'page': 2})

        self.assertEqual(len(second.data['results']), 1)
        first_ids = {row['id'] for row in first.data['results']}
        second_ids = {row['id'] for row in second.data['results']}
        self.assertEqual(first_ids & second_ids, set())

    def test_one_product_can_be_read(self):
        response = self.client.get(self.detail_url(self.cement.pk))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['product_code'], 'CEM-001')

    def test_an_unknown_id_is_a_404(self):
        response = self.client.get(
            self.detail_url('019fd0f4-0000-0000-0000-000000000000')
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # ------------------------------------------------------------------ search

    def test_search_matches_the_name(self):
        response = self.client.get(self.list_url, {'search': 'TMT'})

        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['name'], 'TMT Bar 12mm')

    def test_search_matches_the_product_code(self):
        response = self.client.get(self.list_url, {'search': 'PNT-001'})

        self.assertEqual(response.data['count'], 1)

    def test_search_matches_the_brand(self):
        response = self.client.get(self.list_url, {'search': 'asian'})

        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['brand'], 'Asian Paints')

    def test_search_that_matches_nothing_is_an_empty_page_not_an_error(self):
        response = self.client.get(self.list_url, {'search': 'helicopter'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)

    # ------------------------------------------------------------------ filter

    def test_filter_by_category(self):
        response = self.client.get(self.list_url, {'category': 'steel'})

        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['category'], 'steel')

    def test_filter_by_active(self):
        active = self.client.get(self.list_url, {'active': 'true'})
        inactive = self.client.get(self.list_url, {'active': 'false'})

        self.assertEqual(active.data['count'], 2)
        self.assertEqual(inactive.data['count'], 1)
        self.assertEqual(inactive.data['results'][0]['name'], 'Wall Primer')

    def test_filters_combine(self):
        response = self.client.get(
            self.list_url, {'category': 'cement', 'active': 'true'}
        )

        self.assertEqual(response.data['count'], 1)

    # ---------------------------------------------------------------- ordering

    def test_ordered_by_name_by_default(self):
        response = self.client.get(self.list_url)

        names = [row['name'] for row in response.data['results']]
        self.assertEqual(names, sorted(names))

    def test_ordering_by_price(self):
        response = self.client.get(self.list_url, {'ordering': 'price'})

        prices = [row['selling_price'] for row in response.data['results']]
        self.assertEqual(prices, sorted(prices))

    def test_ordering_by_price_descending(self):
        response = self.client.get(self.list_url, {'ordering': '-price'})

        prices = [row['selling_price'] for row in response.data['results']]
        self.assertEqual(prices, sorted(prices, reverse=True))

    def test_selling_price_is_accepted_as_the_ordering_field_too(self):
        by_alias = self.client.get(self.list_url, {'ordering': 'price'})
        by_column = self.client.get(self.list_url, {'ordering': 'selling_price'})

        self.assertEqual(
            [row['id'] for row in by_alias.data['results']],
            [row['id'] for row in by_column.data['results']],
        )

    def test_ordering_by_created_date(self):
        response = self.client.get(self.list_url, {'ordering': '-created_at'})

        # Newest first, and the paint was made last.
        self.assertEqual(response.data['results'][0]['name'], 'Wall Primer')

    def test_an_unknown_ordering_field_is_ignored_not_an_error(self):
        response = self.client.get(self.list_url, {'ordering': 'nonsense'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 3)


class UpdateTests(ProductTestCase):
    def setUp(self):
        super().setUp()
        self.product = self.make(product_code='CEM-001')

    def test_a_product_can_be_replaced_with_put(self):
        response = self.client.put(
            self.detail_url(self.product.pk),
            payload(name='OPC 43 Grade Cement', selling_price='380.00'),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['name'], 'OPC 43 Grade Cement')
        self.assertEqual(response.data['selling_price'], 380.0)

    def test_a_field_can_be_changed_with_patch(self):
        response = self.client.patch(
            self.detail_url(self.product.pk),
            {'stock_quantity': 12},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['stock_quantity'], 12)
        self.assertEqual(response.data['stock'], StockStatus.LOW_STOCK)
        # Everything else is untouched.
        self.assertEqual(response.data['name'], 'OPC 53 Grade Cement')

    def test_a_patch_that_lifts_the_price_past_the_stored_mrp_is_refused(self):
        """The half that did not arrive comes off the row, or this slips
        through with nothing to compare against."""
        response = self.client.patch(
            self.detail_url(self.product.pk),
            {'selling_price': '999.00'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('selling_price', response.data)

    def test_a_patch_that_lowers_the_mrp_below_the_stored_price_is_refused(self):
        response = self.client.patch(
            self.detail_url(self.product.pk),
            {'mrp': '100.00'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_product_keeps_its_own_code_on_update(self):
        """Its own code is not a duplicate of itself."""
        response = self.client.patch(
            self.detail_url(self.product.pk),
            {'product_code': 'CEM-001', 'name': 'Renamed'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['product_code'], 'CEM-001')

    def test_a_code_belonging_to_another_product_is_refused(self):
        self.make(name='Steel', product_code='STL-001')

        response = self.client.patch(
            self.detail_url(self.product.pk),
            {'product_code': 'STL-001'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('product_code', response.data)

    def test_updated_at_moves(self):
        before = self.product.updated_at

        self.client.patch(
            self.detail_url(self.product.pk),
            {'stock_quantity': 99},
            format='json',
        )
        self.product.refresh_from_db()

        self.assertGreater(self.product.updated_at, before)


class DeleteTests(ProductTestCase):
    def setUp(self):
        super().setUp()
        self.product = self.make(product_code='CEM-001')

    def test_delete_answers_204(self):
        response = self.client.delete(self.detail_url(self.product.pk))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_delete_withdraws_rather_than_erases(self):
        """An order raised last month names this product. The row stays."""
        self.client.delete(self.detail_url(self.product.pk))
        self.product.refresh_from_db()

        self.assertFalse(self.product.active)
        self.assertTrue(Product.objects.filter(pk=self.product.pk).exists())

    def test_a_withdrawn_product_drops_out_of_the_active_list(self):
        self.client.delete(self.detail_url(self.product.pk))

        active = self.client.get(self.list_url, {'active': 'true'})
        everything = self.client.get(self.list_url)

        self.assertEqual(active.data['count'], 0)
        self.assertEqual(everything.data['count'], 1)

    def test_deleting_twice_is_harmless(self):
        first = self.client.delete(self.detail_url(self.product.pk))
        second = self.client.delete(self.detail_url(self.product.pk))

        self.assertEqual(first.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(second.status_code, status.HTTP_204_NO_CONTENT)


class AccessTests(ProductTestCase):
    def setUp(self):
        super().setUp()
        self.product = self.make(product_code='CEM-001')

    def test_the_list_needs_a_token(self):
        self.client.force_authenticate(None)

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_the_detail_needs_a_token(self):
        self.client.force_authenticate(None)

        response = self.client.get(self.detail_url(self.product.pk))

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_creating_without_a_token_is_refused(self):
        self.client.force_authenticate(None)

        response = self.client.post(self.list_url, payload(), format='json')

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_any_signed_in_user_can_read_the_catalogue(self):
        """A field executive raising an order has to see every product."""
        self.client.force_authenticate(self.executive)

        listed = self.client.get(self.list_url)
        detail = self.client.get(self.detail_url(self.product.pk))

        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertEqual(detail.status_code, status.HTTP_200_OK)

    def test_creating_needs_the_master_data_permission(self):
        self.client.force_authenticate(self.executive)

        response = self.client.post(self.list_url, payload(), format='json')

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_updating_needs_the_master_data_permission(self):
        self.client.force_authenticate(self.executive)

        response = self.client.patch(
            self.detail_url(self.product.pk),
            {'selling_price': '1.00'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_deleting_needs_the_master_data_permission(self):
        self.client.force_authenticate(self.executive)

        response = self.client.delete(self.detail_url(self.product.pk))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.product.refresh_from_db()
        self.assertTrue(self.product.active)


class ModelTests(ProductTestCase):
    def test_a_price_survives_the_round_trip_exactly(self):
        product = self.make(selling_price=Decimal('123.45'))
        product.refresh_from_db()

        self.assertEqual(product.selling_price, Decimal('123.45'))

    def test_an_out_of_stock_product_is_not_orderable(self):
        self.assertFalse(self.make(stock_quantity=0).is_orderable)

    def test_an_inactive_product_is_not_orderable(self):
        self.assertFalse(self.make(active=False).is_orderable)

    def test_a_stocked_active_product_is_orderable(self):
        self.assertTrue(self.make().is_orderable)

    def test_discount_on_a_free_product_does_not_divide_by_zero(self):
        product = self.make(mrp=Decimal('0.00'), selling_price=Decimal('0.00'))

        self.assertEqual(product.discount_percent, Decimal('0.00'))
