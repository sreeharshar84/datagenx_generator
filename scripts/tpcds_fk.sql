-- TPC-DS Foreign Key Constraints (MySQL syntax)
-- Based on TPC-DS specification v3.0.1 referential relationships.
--
-- Usage (specify schema on the command line):
--   mysql -u root -p tpcds < scripts/tpcds_fk.sql
--   mysql -u root -p tpcds_sf5 < scripts/tpcds_fk.sql
--
-- Notes:
--   - Run after data is loaded and primary keys are present.
--   - TPC-DS data often uses 0 as an unknown/not-applicable surrogate key.
--     FOREIGN_KEY_CHECKS is disabled while adding constraints so this script can
--     attach relationship metadata to already-loaded benchmark data.
--   - MySQL excludes NULL values from FK checks, but it does not exclude 0.
--   - The local TPC-DS schema uses cr_returned_time_sk and wr_returned_time_sk.

SET FOREIGN_KEY_CHECKS = 0;

-- ============================================================
-- DIMENSION TABLE SELF-REFERENCES
-- ============================================================

-- household_demographics -> income_band
ALTER TABLE household_demographics
ADD CONSTRAINT HD_FK1 FOREIGN KEY (hd_income_band_sk) REFERENCES income_band(ib_income_band_sk);

-- customer -> customer_address
ALTER TABLE customer
ADD CONSTRAINT CUSTOMER_FK1 FOREIGN KEY (c_current_addr_sk) REFERENCES customer_address(ca_address_sk);

-- customer -> customer_demographics
ALTER TABLE customer
ADD CONSTRAINT CUSTOMER_FK2 FOREIGN KEY (c_current_cdemo_sk) REFERENCES customer_demographics(cd_demo_sk);

-- customer -> household_demographics
ALTER TABLE customer
ADD CONSTRAINT CUSTOMER_FK3 FOREIGN KEY (c_current_hdemo_sk) REFERENCES household_demographics(hd_demo_sk);

-- promotion -> item
ALTER TABLE promotion
ADD CONSTRAINT PROMOTION_FK1 FOREIGN KEY (p_item_sk) REFERENCES item(i_item_sk);

-- promotion -> date_dim (start)
ALTER TABLE promotion
ADD CONSTRAINT PROMOTION_FK2 FOREIGN KEY (p_start_date_sk) REFERENCES date_dim(d_date_sk);

-- promotion -> date_dim (end)
ALTER TABLE promotion
ADD CONSTRAINT PROMOTION_FK3 FOREIGN KEY (p_end_date_sk) REFERENCES date_dim(d_date_sk);

-- ============================================================
-- STORE_SALES
-- ============================================================

ALTER TABLE store_sales
ADD CONSTRAINT SS_FK1 FOREIGN KEY (ss_sold_date_sk) REFERENCES date_dim(d_date_sk);

ALTER TABLE store_sales
ADD CONSTRAINT SS_FK2 FOREIGN KEY (ss_sold_time_sk) REFERENCES time_dim(t_time_sk);

ALTER TABLE store_sales
ADD CONSTRAINT SS_FK3 FOREIGN KEY (ss_item_sk) REFERENCES item(i_item_sk);

ALTER TABLE store_sales
ADD CONSTRAINT SS_FK4 FOREIGN KEY (ss_customer_sk) REFERENCES customer(c_customer_sk);

ALTER TABLE store_sales
ADD CONSTRAINT SS_FK5 FOREIGN KEY (ss_cdemo_sk) REFERENCES customer_demographics(cd_demo_sk);

ALTER TABLE store_sales
ADD CONSTRAINT SS_FK6 FOREIGN KEY (ss_hdemo_sk) REFERENCES household_demographics(hd_demo_sk);

ALTER TABLE store_sales
ADD CONSTRAINT SS_FK7 FOREIGN KEY (ss_addr_sk) REFERENCES customer_address(ca_address_sk);

ALTER TABLE store_sales
ADD CONSTRAINT SS_FK8 FOREIGN KEY (ss_store_sk) REFERENCES store(s_store_sk);

ALTER TABLE store_sales
ADD CONSTRAINT SS_FK9 FOREIGN KEY (ss_promo_sk) REFERENCES promotion(p_promo_sk);

-- ============================================================
-- STORE_RETURNS
-- ============================================================

ALTER TABLE store_returns
ADD CONSTRAINT SR_FK1 FOREIGN KEY (sr_returned_date_sk) REFERENCES date_dim(d_date_sk);

ALTER TABLE store_returns
ADD CONSTRAINT SR_FK2 FOREIGN KEY (sr_return_time_sk) REFERENCES time_dim(t_time_sk);

ALTER TABLE store_returns
ADD CONSTRAINT SR_FK3 FOREIGN KEY (sr_item_sk) REFERENCES item(i_item_sk);

ALTER TABLE store_returns
ADD CONSTRAINT SR_FK4 FOREIGN KEY (sr_customer_sk) REFERENCES customer(c_customer_sk);

ALTER TABLE store_returns
ADD CONSTRAINT SR_FK5 FOREIGN KEY (sr_cdemo_sk) REFERENCES customer_demographics(cd_demo_sk);

ALTER TABLE store_returns
ADD CONSTRAINT SR_FK6 FOREIGN KEY (sr_hdemo_sk) REFERENCES household_demographics(hd_demo_sk);

ALTER TABLE store_returns
ADD CONSTRAINT SR_FK7 FOREIGN KEY (sr_addr_sk) REFERENCES customer_address(ca_address_sk);

ALTER TABLE store_returns
ADD CONSTRAINT SR_FK8 FOREIGN KEY (sr_store_sk) REFERENCES store(s_store_sk);

ALTER TABLE store_returns
ADD CONSTRAINT SR_FK9 FOREIGN KEY (sr_reason_sk) REFERENCES reason(r_reason_sk);

-- store_returns references store_sales (composite FK)
ALTER TABLE store_returns
ADD CONSTRAINT SR_FK10 FOREIGN KEY (sr_item_sk, sr_ticket_number)
    REFERENCES store_sales(ss_item_sk, ss_ticket_number);

-- ============================================================
-- CATALOG_SALES
-- ============================================================

ALTER TABLE catalog_sales
ADD CONSTRAINT CS_FK1 FOREIGN KEY (cs_sold_date_sk) REFERENCES date_dim(d_date_sk);

ALTER TABLE catalog_sales
ADD CONSTRAINT CS_FK2 FOREIGN KEY (cs_sold_time_sk) REFERENCES time_dim(t_time_sk);

ALTER TABLE catalog_sales
ADD CONSTRAINT CS_FK3 FOREIGN KEY (cs_item_sk) REFERENCES item(i_item_sk);

ALTER TABLE catalog_sales
ADD CONSTRAINT CS_FK4 FOREIGN KEY (cs_bill_customer_sk) REFERENCES customer(c_customer_sk);

ALTER TABLE catalog_sales
ADD CONSTRAINT CS_FK5 FOREIGN KEY (cs_bill_cdemo_sk) REFERENCES customer_demographics(cd_demo_sk);

ALTER TABLE catalog_sales
ADD CONSTRAINT CS_FK6 FOREIGN KEY (cs_bill_hdemo_sk) REFERENCES household_demographics(hd_demo_sk);

ALTER TABLE catalog_sales
ADD CONSTRAINT CS_FK7 FOREIGN KEY (cs_bill_addr_sk) REFERENCES customer_address(ca_address_sk);

ALTER TABLE catalog_sales
ADD CONSTRAINT CS_FK8 FOREIGN KEY (cs_ship_customer_sk) REFERENCES customer(c_customer_sk);

ALTER TABLE catalog_sales
ADD CONSTRAINT CS_FK9 FOREIGN KEY (cs_ship_cdemo_sk) REFERENCES customer_demographics(cd_demo_sk);

ALTER TABLE catalog_sales
ADD CONSTRAINT CS_FK10 FOREIGN KEY (cs_ship_hdemo_sk) REFERENCES household_demographics(hd_demo_sk);

ALTER TABLE catalog_sales
ADD CONSTRAINT CS_FK11 FOREIGN KEY (cs_ship_addr_sk) REFERENCES customer_address(ca_address_sk);

ALTER TABLE catalog_sales
ADD CONSTRAINT CS_FK12 FOREIGN KEY (cs_call_center_sk) REFERENCES call_center(cc_call_center_sk);

ALTER TABLE catalog_sales
ADD CONSTRAINT CS_FK13 FOREIGN KEY (cs_catalog_page_sk) REFERENCES catalog_page(cp_catalog_page_sk);

ALTER TABLE catalog_sales
ADD CONSTRAINT CS_FK14 FOREIGN KEY (cs_ship_mode_sk) REFERENCES ship_mode(sm_ship_mode_sk);

ALTER TABLE catalog_sales
ADD CONSTRAINT CS_FK15 FOREIGN KEY (cs_warehouse_sk) REFERENCES warehouse(w_warehouse_sk);

ALTER TABLE catalog_sales
ADD CONSTRAINT CS_FK16 FOREIGN KEY (cs_promo_sk) REFERENCES promotion(p_promo_sk);

-- ============================================================
-- CATALOG_RETURNS
-- ============================================================

ALTER TABLE catalog_returns
ADD CONSTRAINT CR_FK1 FOREIGN KEY (cr_returned_date_sk) REFERENCES date_dim(d_date_sk);

ALTER TABLE catalog_returns
ADD CONSTRAINT CR_FK2 FOREIGN KEY (cr_returned_time_sk) REFERENCES time_dim(t_time_sk);

ALTER TABLE catalog_returns
ADD CONSTRAINT CR_FK3 FOREIGN KEY (cr_item_sk) REFERENCES item(i_item_sk);

ALTER TABLE catalog_returns
ADD CONSTRAINT CR_FK4 FOREIGN KEY (cr_refunded_customer_sk) REFERENCES customer(c_customer_sk);

ALTER TABLE catalog_returns
ADD CONSTRAINT CR_FK5 FOREIGN KEY (cr_refunded_cdemo_sk) REFERENCES customer_demographics(cd_demo_sk);

ALTER TABLE catalog_returns
ADD CONSTRAINT CR_FK6 FOREIGN KEY (cr_refunded_hdemo_sk) REFERENCES household_demographics(hd_demo_sk);

ALTER TABLE catalog_returns
ADD CONSTRAINT CR_FK7 FOREIGN KEY (cr_refunded_addr_sk) REFERENCES customer_address(ca_address_sk);

ALTER TABLE catalog_returns
ADD CONSTRAINT CR_FK8 FOREIGN KEY (cr_returning_customer_sk) REFERENCES customer(c_customer_sk);

ALTER TABLE catalog_returns
ADD CONSTRAINT CR_FK9 FOREIGN KEY (cr_returning_cdemo_sk) REFERENCES customer_demographics(cd_demo_sk);

ALTER TABLE catalog_returns
ADD CONSTRAINT CR_FK10 FOREIGN KEY (cr_returning_hdemo_sk) REFERENCES household_demographics(hd_demo_sk);

ALTER TABLE catalog_returns
ADD CONSTRAINT CR_FK11 FOREIGN KEY (cr_returning_addr_sk) REFERENCES customer_address(ca_address_sk);

ALTER TABLE catalog_returns
ADD CONSTRAINT CR_FK12 FOREIGN KEY (cr_call_center_sk) REFERENCES call_center(cc_call_center_sk);

ALTER TABLE catalog_returns
ADD CONSTRAINT CR_FK13 FOREIGN KEY (cr_catalog_page_sk) REFERENCES catalog_page(cp_catalog_page_sk);

ALTER TABLE catalog_returns
ADD CONSTRAINT CR_FK14 FOREIGN KEY (cr_ship_mode_sk) REFERENCES ship_mode(sm_ship_mode_sk);

ALTER TABLE catalog_returns
ADD CONSTRAINT CR_FK15 FOREIGN KEY (cr_warehouse_sk) REFERENCES warehouse(w_warehouse_sk);

ALTER TABLE catalog_returns
ADD CONSTRAINT CR_FK16 FOREIGN KEY (cr_reason_sk) REFERENCES reason(r_reason_sk);

-- catalog_returns references catalog_sales (composite FK)
ALTER TABLE catalog_returns
ADD CONSTRAINT CR_FK17 FOREIGN KEY (cr_item_sk, cr_order_number)
    REFERENCES catalog_sales(cs_item_sk, cs_order_number);

-- ============================================================
-- WEB_SALES
-- ============================================================

ALTER TABLE web_sales
ADD CONSTRAINT WS_FK1 FOREIGN KEY (ws_sold_date_sk) REFERENCES date_dim(d_date_sk);

ALTER TABLE web_sales
ADD CONSTRAINT WS_FK2 FOREIGN KEY (ws_sold_time_sk) REFERENCES time_dim(t_time_sk);

ALTER TABLE web_sales
ADD CONSTRAINT WS_FK3 FOREIGN KEY (ws_item_sk) REFERENCES item(i_item_sk);

ALTER TABLE web_sales
ADD CONSTRAINT WS_FK4 FOREIGN KEY (ws_bill_customer_sk) REFERENCES customer(c_customer_sk);

ALTER TABLE web_sales
ADD CONSTRAINT WS_FK5 FOREIGN KEY (ws_bill_cdemo_sk) REFERENCES customer_demographics(cd_demo_sk);

ALTER TABLE web_sales
ADD CONSTRAINT WS_FK6 FOREIGN KEY (ws_bill_hdemo_sk) REFERENCES household_demographics(hd_demo_sk);

ALTER TABLE web_sales
ADD CONSTRAINT WS_FK7 FOREIGN KEY (ws_bill_addr_sk) REFERENCES customer_address(ca_address_sk);

ALTER TABLE web_sales
ADD CONSTRAINT WS_FK8 FOREIGN KEY (ws_ship_customer_sk) REFERENCES customer(c_customer_sk);

ALTER TABLE web_sales
ADD CONSTRAINT WS_FK9 FOREIGN KEY (ws_ship_cdemo_sk) REFERENCES customer_demographics(cd_demo_sk);

ALTER TABLE web_sales
ADD CONSTRAINT WS_FK10 FOREIGN KEY (ws_ship_hdemo_sk) REFERENCES household_demographics(hd_demo_sk);

ALTER TABLE web_sales
ADD CONSTRAINT WS_FK11 FOREIGN KEY (ws_ship_addr_sk) REFERENCES customer_address(ca_address_sk);

ALTER TABLE web_sales
ADD CONSTRAINT WS_FK12 FOREIGN KEY (ws_web_page_sk) REFERENCES web_page(wp_web_page_sk);

ALTER TABLE web_sales
ADD CONSTRAINT WS_FK13 FOREIGN KEY (ws_web_site_sk) REFERENCES web_site(web_site_sk);

ALTER TABLE web_sales
ADD CONSTRAINT WS_FK14 FOREIGN KEY (ws_ship_mode_sk) REFERENCES ship_mode(sm_ship_mode_sk);

ALTER TABLE web_sales
ADD CONSTRAINT WS_FK15 FOREIGN KEY (ws_warehouse_sk) REFERENCES warehouse(w_warehouse_sk);

ALTER TABLE web_sales
ADD CONSTRAINT WS_FK16 FOREIGN KEY (ws_promo_sk) REFERENCES promotion(p_promo_sk);

-- ============================================================
-- WEB_RETURNS
-- ============================================================

ALTER TABLE web_returns
ADD CONSTRAINT WR_FK1 FOREIGN KEY (wr_returned_date_sk) REFERENCES date_dim(d_date_sk);

ALTER TABLE web_returns
ADD CONSTRAINT WR_FK2 FOREIGN KEY (wr_returned_time_sk) REFERENCES time_dim(t_time_sk);

ALTER TABLE web_returns
ADD CONSTRAINT WR_FK3 FOREIGN KEY (wr_item_sk) REFERENCES item(i_item_sk);

ALTER TABLE web_returns
ADD CONSTRAINT WR_FK4 FOREIGN KEY (wr_refunded_customer_sk) REFERENCES customer(c_customer_sk);

ALTER TABLE web_returns
ADD CONSTRAINT WR_FK5 FOREIGN KEY (wr_refunded_cdemo_sk) REFERENCES customer_demographics(cd_demo_sk);

ALTER TABLE web_returns
ADD CONSTRAINT WR_FK6 FOREIGN KEY (wr_refunded_hdemo_sk) REFERENCES household_demographics(hd_demo_sk);

ALTER TABLE web_returns
ADD CONSTRAINT WR_FK7 FOREIGN KEY (wr_refunded_addr_sk) REFERENCES customer_address(ca_address_sk);

ALTER TABLE web_returns
ADD CONSTRAINT WR_FK8 FOREIGN KEY (wr_returning_customer_sk) REFERENCES customer(c_customer_sk);

ALTER TABLE web_returns
ADD CONSTRAINT WR_FK9 FOREIGN KEY (wr_returning_cdemo_sk) REFERENCES customer_demographics(cd_demo_sk);

ALTER TABLE web_returns
ADD CONSTRAINT WR_FK10 FOREIGN KEY (wr_returning_hdemo_sk) REFERENCES household_demographics(hd_demo_sk);

ALTER TABLE web_returns
ADD CONSTRAINT WR_FK11 FOREIGN KEY (wr_returning_addr_sk) REFERENCES customer_address(ca_address_sk);

ALTER TABLE web_returns
ADD CONSTRAINT WR_FK12 FOREIGN KEY (wr_web_page_sk) REFERENCES web_page(wp_web_page_sk);

ALTER TABLE web_returns
ADD CONSTRAINT WR_FK13 FOREIGN KEY (wr_reason_sk) REFERENCES reason(r_reason_sk);

-- web_returns references web_sales (composite FK)
ALTER TABLE web_returns
ADD CONSTRAINT WR_FK14 FOREIGN KEY (wr_item_sk, wr_order_number)
    REFERENCES web_sales(ws_item_sk, ws_order_number);

-- ============================================================
-- INVENTORY
-- ============================================================

ALTER TABLE inventory
ADD CONSTRAINT INV_FK1 FOREIGN KEY (inv_date_sk) REFERENCES date_dim(d_date_sk);

ALTER TABLE inventory
ADD CONSTRAINT INV_FK2 FOREIGN KEY (inv_item_sk) REFERENCES item(i_item_sk);

ALTER TABLE inventory
ADD CONSTRAINT INV_FK3 FOREIGN KEY (inv_warehouse_sk) REFERENCES warehouse(w_warehouse_sk);

SET FOREIGN_KEY_CHECKS = 1;
