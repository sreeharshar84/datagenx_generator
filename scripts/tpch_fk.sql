-- TPC-H Foreign Key Constraints (MySQL syntax)
-- Based on dss.ri from TPC-H specification
--
-- Usage (specify schema on command line):
--   mysql -u root tpch_sf5 < scripts/tpch_fk.sql
--   mysql -u root tpch_sf10 < scripts/tpch_fk.sql

SET FOREIGN_KEY_CHECKS = 0;

-- NATION -> REGION
ALTER TABLE nation
ADD CONSTRAINT NATION_FK1 FOREIGN KEY (n_regionkey) REFERENCES region(r_regionkey);

-- SUPPLIER -> NATION
ALTER TABLE supplier
ADD CONSTRAINT SUPPLIER_FK1 FOREIGN KEY (s_nationkey) REFERENCES nation(n_nationkey);

-- CUSTOMER -> NATION
ALTER TABLE customer
ADD CONSTRAINT CUSTOMER_FK1 FOREIGN KEY (c_nationkey) REFERENCES nation(n_nationkey);

-- PARTSUPP -> SUPPLIER
ALTER TABLE partsupp
ADD CONSTRAINT PARTSUPP_FK1 FOREIGN KEY (ps_suppkey) REFERENCES supplier(s_suppkey);

-- PARTSUPP -> PART
ALTER TABLE partsupp
ADD CONSTRAINT PARTSUPP_FK2 FOREIGN KEY (ps_partkey) REFERENCES part(p_partkey);

-- ORDERS -> CUSTOMER
ALTER TABLE orders
ADD CONSTRAINT ORDERS_FK1 FOREIGN KEY (o_custkey) REFERENCES customer(c_custkey);

-- LINEITEM -> ORDERS
ALTER TABLE lineitem
ADD CONSTRAINT LINEITEM_FK1 FOREIGN KEY (l_orderkey) REFERENCES orders(o_orderkey);

-- LINEITEM -> PARTSUPP (composite FK)
ALTER TABLE lineitem
ADD CONSTRAINT LINEITEM_FK2 FOREIGN KEY (l_partkey, l_suppkey) REFERENCES partsupp(ps_partkey, ps_suppkey);

SET FOREIGN_KEY_CHECKS = 1;
