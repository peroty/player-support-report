import unittest

from app import app


class ErrorHandlingTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_unknown_route_returns_404(self):
        response = self.client.get('/this-route-does-not-exist')
        self.assertEqual(response.status_code, 404)


if __name__ == '__main__':
    unittest.main()
