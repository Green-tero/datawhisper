-- DataWhisper Demo Schema
-- E-commerce store scenario with customers, orders, order items, and products.

CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    country TEXT NOT NULL,
    signup_date TEXT NOT NULL
);

CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    price REAL NOT NULL,
    stock INTEGER NOT NULL
);

CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    order_date TEXT NOT NULL,
    status TEXT NOT NULL,
    total_amount REAL NOT NULL
);

CREATE TABLE order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL,
    unit_price REAL NOT NULL
);

-- Seed data
INSERT INTO customers VALUES
    (1, 'Alice Smith', 'alice@example.com', 'US', '2025-01-15'),
    (2, 'Bob Jones', 'bob@example.co.uk', 'UK', '2025-02-10'),
    (3, 'Charlie Brown', 'charlie@example.org', 'CA', '2025-03-01'),
    (4, 'Diana Prince', 'diana@example.com', 'US', '2025-03-22');

INSERT INTO products VALUES
    (1, 'Ergonomic Desk Chair', 'Furniture', 249.99, 45),
    (2, 'Mechanical Keyboard', 'Electronics', 119.50, 120),
    (3, 'Ultra-wide Monitor', 'Electronics', 399.00, 30),
    (4, 'Desk Lamp', 'Lighting', 45.00, 80);

INSERT INTO orders VALUES
    (1, 1, '2025-04-01', 'paid', 369.49),
    (2, 2, '2025-04-05', 'paid', 119.50),
    (3, 1, '2025-04-12', 'pending', 45.00),
    (4, 3, '2025-04-18', 'cancelled', 399.00),
    (5, 4, '2025-04-20', 'paid', 249.99);

INSERT INTO order_items VALUES
    (1, 1, 1, 1, 249.99),
    (2, 1, 2, 1, 119.50),
    (3, 2, 2, 1, 119.50),
    (4, 3, 4, 1, 45.00),
    (5, 4, 3, 1, 399.00),
    (6, 5, 1, 1, 249.99);
