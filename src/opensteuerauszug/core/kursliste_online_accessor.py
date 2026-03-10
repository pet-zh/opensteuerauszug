import logging
import requests
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Optional, Type, Union
from opensteuerauszug.model.kursliste import (
    Security,
    Share,
    Bond,
    Fund,
    PaymentBond,
    PaymentFund,
    PaymentShare,
    PaymentTypeESTV,
    Yearend,
    Derivative,
    CoinBullion,
    CurrencyNote,
    LiborSwap,
    SecurityGroupESTV,
    SecurityTypeESTV,
)
from opensteuerauszug.kursliste.downloader import _initialize_session

logger = logging.getLogger(__name__)

BASE_URL = "https://www.ictax.admin.ch/extern/api"
SECURITY_ENDPOINT = f"{BASE_URL}/security/security.json"
EXCHANGE_RATE_ENDPOINT = f"{BASE_URL}/coreGadget/exchangeRates.json"
POSITIVE_INT_MASK = 0x7FFFFFFF


class OnlineKurslisteAccessor:
    """Provides Kursliste data via ICTax website with session-based caching.

    For security lookups, fetches data from the ICTax API. For DA-1 rates and
    sign data (not available via API), delegates to a fallback accessor if provided.
    """

    def __init__(
        self,
        tax_year: int,
        session: Optional[requests.Session] = None,
        fallback_accessor=None,
    ):
        """
        Initialize the OnlineKurslisteAccessor.

        Args:
            tax_year: The tax year for context
            session: Optional requests.Session to reuse (e.g., with existing CSRF token)
            fallback_accessor: Optional KurslisteAccessor for DA-1 rates and sign data
        """
        self.tax_year = tax_year
        self.session = session or requests.Session()
        self.fallback_accessor = fallback_accessor
        if session is None:
            logger.info("Creating new session and initializing with CSRF token")
            _initialize_session(self.session)
        else:
            logger.info("Reusing provided session (assumed to be initialized)")

    def get_security_by_isin(self, isin: str) -> Optional[Security]:
        """Fetch security by ISIN (cached per session)."""
        return self._fetch_security_by_isin(isin)

    def get_security_by_valor(self, valor_number: int) -> Optional[Security]:
        """Fetch security by valor number (cached per session)."""
        return self._fetch_security_by_valor(valor_number)

    @lru_cache(maxsize=None)
    def _fetch_security_by_isin(self, isin: str) -> Optional[Security]:
        """Internal cached method for ISIN lookups."""
        return self._fetch_security(isin=isin)

    @lru_cache(maxsize=None)
    def _fetch_security_by_valor(self, valor_number: int) -> Optional[Security]:
        """Internal cached method for valor lookups."""
        return self._fetch_security(valor_number=valor_number)

    def _fetch_security(
        self, isin: Optional[str] = None, valor_number: Optional[int] = None
    ) -> Optional[Security]:
        """
        Fetch security from API by ISIN or valor number.
        Both endpoints return full data; caching works for both lookup paths.

        Uses the tax year's year-end date (December 31) as the reference date
        to fetch the correct year-end values and dividend totals.
        """
        try:
            # Use tax year's year-end date as reference date (Dec 31 of tax year)
            # This ensures we get the correct year-end tax values and dividend totals
            reference_date = date(self.tax_year, 12, 31)

            if not isin and valor_number is None:
                return None

            payload = {
                "isin": isin if isin else None,
                "valorNumber": valor_number if valor_number is not None else None,
                "referenceDate": self._date_to_ms(reference_date),
                "language": "en",
                "isCantonBL": False,
            }

            if isin:
                logger.info(
                    f"Fetching security by ISIN: {isin} for tax year {self.tax_year}"
                )
            elif valor_number is not None:
                logger.info(
                    f"Fetching security by valor: {valor_number} for tax year {self.tax_year}"
                )

            response = self.session.post(SECURITY_ENDPOINT, json=payload, timeout=10)

            if response.status_code == 403:
                logger.warning("403 Forbidden - attempting session reinitialization")
                _initialize_session(self.session)
                response = self.session.post(
                    SECURITY_ENDPOINT, json=payload, timeout=10
                )
                if response.status_code == 403:
                    logger.warning("Still getting 403 after reinitialization")
                    return None

            response.raise_for_status()

            data = response.json()
            if data.get("status") != "SUCCESS":
                logger.warning(f"API error: {data.get('error')}")
                return None

            # Parse response
            calc = data.get("data", {}).get("calculation", {})
            if not calc:
                logger.warning("No calculation data in response for security")
                return None

            # Extract calculationDetails for individual payment entries
            calc_details = data.get("data", {}).get("calculationDetails", {})

            security = self._build_security_model(calc, calc_details)
            if security:
                logger.info(
                    f"Fetched security: {security.securityName} ({security.isin})"
                )
            return security

        except Exception as e:
            logger.error(f"Error fetching security: {e}", exc_info=True)
            return None

    def _build_security_model(
        self, calc: dict, calc_details: Optional[dict] = None
    ) -> Optional[Security]:
        """Convert API calculation response to appropriate Security model.

        Args:
            calc: The calculation object from the API response.
            calc_details: The calculationDetails object containing individual
                payment entries (optional, used for extracting actual dividend dates).
        """
        instrument_group = calc.get("instrumentGroup")

        # Map instrumentGroup to model type and SecurityGroupESTV
        type_map: dict[
            str, tuple[Type[Security], SecurityGroupESTV, Optional[SecurityTypeESTV]]
        ] = {
            "SHARE": (Share, SecurityGroupESTV.SHARE, SecurityTypeESTV.SHARE_COMMON),
            "BOND": (Bond, SecurityGroupESTV.BOND, SecurityTypeESTV.BOND_BOND),
            "FUND": (Fund, SecurityGroupESTV.FUND, SecurityTypeESTV.FUND_DISTRIBUTION),
            "DERIVATIVE": (Derivative, SecurityGroupESTV.DEVT, SecurityTypeESTV.DEVT_INDEXBASKET),
            "COIN_BULLION": (CoinBullion, SecurityGroupESTV.COINBULL, SecurityTypeESTV.COINBULL_GOLD),
            "CURRENCY_NOTE": (CurrencyNote, SecurityGroupESTV.CURRNOTE, SecurityTypeESTV.CURRNOTE_CURRENCY),
            "LIBOR_SWAP": (LiborSwap, SecurityGroupESTV.LIBOSWAP, SecurityTypeESTV.LIBOSWAP_SWAP),
        }

        security_info = type_map.get(instrument_group)
        if not security_info:
            raise ValueError(f"Unsupported instrument group '{instrument_group}' in API response")

        security_model_class, security_group, security_type = security_info

        # Build security object from calculation
        try:
            # Use security ID or valor number as ID
            security_id = calc.get("securityId") or calc.get("valorNumber")
            if security_id is None:
                logger.warning("No securityId or valorNumber found in calc data")
                return None

            security_name = self._normalize_security_name(calc.get("name"))
            name_parts = self._extract_name_parts(calc.get("name"))
            institution_name = name_parts[0] if name_parts else security_name
            security_appendix = None
            institution_appendix = None

            # Prepare common parameters for all security types
            common_params = {
                "id": int(security_id),
                "valorNumber": calc.get("valorNumber"),
                "isin": calc.get("isin"),
                "securityGroup": security_group,
                "securityType": security_type,
                "securityName": security_name,
            }

            # Extract currency - can be a string or a dict with a 'shortName' field
            currency_raw = calc.get("currency", "CHF")
            if isinstance(currency_raw, dict):
                currency = currency_raw.get("shortName", "CHF")
            else:
                currency = str(currency_raw) if currency_raw else "CHF"

            # Extract country code from ISIN (first 2 characters are the country code)
            isin = calc.get("isin")
            if isin and len(isin) >= 2:
                country_code = isin[:2].upper()
            else:
                country_code = currency[:2] if currency else None

            # Add shared fields for all securities with country/currency attributes
            if security_model_class in {
                Share,
                Bond,
                Fund,
                Derivative,
                CoinBullion,
                CurrencyNote,
                LiborSwap,
            }:
                common_params.update({"country": country_code, "currency": currency})

            # Add institution fields for security classes that require them
            if security_model_class in {Share, Bond, Fund, Derivative}:
                common_params.update(
                    {
                        "institutionId": 1,
                        "institutionName": institution_name,
                        "nominalValue": Decimal("0"),
                    }
                )

            if security_appendix:
                common_params["securityAppendix"] = security_appendix

            if security_model_class in {Bond, Fund} and institution_appendix:
                common_params["institutionAppendix"] = institution_appendix

            if security_model_class == Share:
                common_params["securityName"] = None
                security_appendix = name_parts[1] if len(name_parts) >= 2 else None
                if security_appendix:
                    common_params["securityAppendix"] = security_appendix
                common_params["securityType"] = self._infer_share_type(
                    security_appendix
                )

            if security_model_class == Fund:
                duplicate_name_pattern = (
                    len(name_parts) >= 3 and name_parts[0] == name_parts[1]
                )
                if duplicate_name_pattern:
                    common_params["securityName"] = name_parts[0]
                    common_params["institutionName"] = name_parts[0]
                else:
                    common_params["securityName"] = None
                    if len(name_parts) >= 2:
                        common_params["securityAppendix"] = name_parts[1]
                    inst_name, inst_appendix = (
                        self._split_institution_name_and_appendix(
                            name_parts[0] if name_parts else ""
                        )
                    )
                    if inst_name:
                        common_params["institutionName"] = inst_name
                    if inst_appendix:
                        common_params["institutionAppendix"] = inst_appendix
                common_params["securityType"] = self._infer_fund_type(name_parts)

            security = security_model_class(**common_params)

            # Extract and add yearend/payment data
            if security_model_class == Share and isinstance(security, Share):
                yearend_list = self._extract_yearend_data(
                    calc,
                    calc_details=calc_details,
                    include_tax_value=(currency == "CHF"),
                )
                if yearend_list:
                    security.yearend = yearend_list

                payment_list = self._extract_payment_data(
                    calc,
                    calc_details,
                    payment_model=PaymentShare,
                    infer_with_holding_from_sign=True,
                )
                if payment_list:
                    security.payment = payment_list

            if security_model_class == Fund and isinstance(security, Fund):
                yearend_entry = self._extract_yearend_data(
                    calc,
                    calc_details=calc_details,
                    yearend_model=Yearend,
                    as_list=False,
                    include_tax_value=(currency == "CHF"),
                )
                if yearend_entry:
                    security.yearend = yearend_entry

                payment_list = self._extract_payment_data(
                    calc,
                    calc_details,
                    payment_model=PaymentFund,
                    infer_with_holding_from_sign=False,
                )
                if payment_list:
                    security.payment = payment_list

                if security.securityType == SecurityTypeESTV.FUND_ACCUMULATION:
                    security.sign = "(N)"
                    for payment in security.payment:
                        payment.paymentType = PaymentTypeESTV.FUND_ACCUMULATION
                        payment.taxEvent = True

            return security
        except Exception as e:
            logger.error(f"Error building security model: {e}", exc_info=True)
            return None

    def get_exchange_rate(
        self, currency: str, reference_date: date
    ) -> Optional[Decimal]:
        """
        Fetch exchange rate for currency on reference date.

        The ICTax website may only have exchange rates for specific dates (business days).
        This method first tries the exact date, then falls back to the previous day.
        """
        if currency == "CHF":
            return Decimal("1")

        # Try exact date first
        rate = self._fetch_exchange_rate(currency, reference_date)
        if rate is not None:
            return rate

        # Fall back to previous day when a date-specific rate is unavailable.
        previous_day = reference_date - timedelta(days=1)
        rate = self._fetch_exchange_rate(currency, previous_day)
        if rate is not None:
            return rate

        return None

    @lru_cache(maxsize=None)
    def _fetch_exchange_rate(
        self, currency: str, reference_date: date
    ) -> Optional[Decimal]:
        """Internal cached method for exchange rate lookups."""
        try:
            payload = {
                "from": 0,
                "size": 10,
                "sort": [],
                "referenceDate": self._date_to_ms(reference_date),
                "currencies": [],  # Empty = all default currencies
            }

            response = self.session.post(
                EXCHANGE_RATE_ENDPOINT, json=payload, timeout=10
            )

            if response.status_code == 403:
                logger.warning(
                    "403 Forbidden fetching exchange rates - reinitializing session"
                )
                _initialize_session(self.session)
                response = self.session.post(
                    EXCHANGE_RATE_ENDPOINT, json=payload, timeout=10
                )
                if response.status_code == 403:
                    logger.warning("Still getting 403 after reinitialization")
                    return None

            response.raise_for_status()

            data = response.json()
            if data.get("status") != "SUCCESS":
                logger.warning(f"API error: {data.get('error')}")
                return None

            # Search currencies array
            currencies_data = data.get("data", {}).get("currencies", [])

            for curr_data in currencies_data:
                curr_short_name = curr_data.get("currency", {}).get("shortName")
                if curr_short_name == currency:
                    value = curr_data.get("value")
                    denomination = curr_data.get("denomination", 1)
                    if value is not None:
                        rate = Decimal(str(value)) / Decimal(str(denomination))
                        return rate

            return None

        except Exception as e:
            logger.error(
                f"Error fetching exchange rate for {currency}: {e}", exc_info=True
            )
            return None

    def _extract_yearend_data(
        self,
        calc: dict,
        calc_details: Optional[dict] = None,
        yearend_model=None,
        as_list: bool = True,
        include_tax_value: bool = True,
    ) -> Union[list, Yearend, None]:
        """Extract yearend/tax value data from API calculation response.

        Example real API response structure:
        {
            "taxValueTotalChf": 226.94,
            "paymentValueTotalChfWithHoldingTax": 0,
            "paymentValueTotalChfWithoutHoldingTax": 0.87703,
            "currency": {"categoryShortName": "CURREN", "shortName": "USD"},
            "valorNumber": 908440,
            "isin": "US0378331005",
            "name": "Apple Inc., Stammaktien, US",
            ...
        }
        """
        from opensteuerauszug.model.kursliste import YearendGrossNet, QuotationType

        if yearend_model is None:
            yearend_model = YearendGrossNet

        yearend_list = []

        try:
            tax_value_chf = calc.get("taxValueTotalChf")
            detailed_tax_value_chf = None
            if calc_details:
                entries = calc_details.get("entries", [])
                tax_entries = [e for e in entries if e.get("type") == "TAX_VALUE"]
                if tax_entries:
                    tax_entry = tax_entries[0]
                    detailed_tax_value_chf = tax_entry.get("valueChf")

            if detailed_tax_value_chf is not None:
                tax_value_chf = detailed_tax_value_chf

            if tax_value_chf is not None:
                # Create a YearendGrossNet entry with the tax value
                # quotationType must be PERCENT or PIECE, defaulting to PIECE for share prices
                # id is required by Entity base class, use a hash to generate unique ID
                yearend_id = (
                    hash(f"yearend_{self.tax_year}_{tax_value_chf}")
                    & POSITIVE_INT_MASK
                )

                yearend_params = {
                    "id": yearend_id,
                    "quotationType": QuotationType.PIECE,
                    "taxValueCHF": Decimal(str(tax_value_chf)),
                }
                if include_tax_value:
                    yearend_params["taxValue"] = Decimal(str(tax_value_chf))

                yearend_entry = yearend_model(**yearend_params)
                if as_list:
                    yearend_list.append(yearend_entry)
                else:
                    return yearend_entry

        except Exception as e:
            logger.warning(f"Error extracting yearend data: {e}", exc_info=True)

        if as_list:
            return yearend_list
        return None

    def _extract_payment_data(
        self,
        calc: dict,
        calc_details: Optional[dict] = None,
        payment_model: Union[
            Type[PaymentShare], Type[PaymentFund], Type[PaymentBond]
        ] = PaymentShare,
        infer_with_holding_from_sign: bool = True,
    ) -> list:
        """Extract payment/dividend data from API calculation response.

        The real ICTax API provides individual payment entries in calculationDetails.entries
        with type="PAYMENT". Each entry contains:
        - paymentDate: milliseconds timestamp of the actual payment date
        - exDate: milliseconds timestamp of the ex-dividend date
        - value: payment value in original currency
        - valueChf: payment value in CHF
        - exchangeRate: exchange rate used for conversion
        - sign: tax treatment indicator (e.g., "(Q)" for foreign withholding tax)
        - currency: dict with shortName (e.g., {"shortName": "USD"})

        If calculationDetails is not available, falls back to summary values at the
        top level (paymentValueTotalChfWithHoldingTax, paymentValueTotalChfWithoutHoldingTax)
        with year-end date as placeholder.

        Sign values for tax treatment:
        - "(Q)" = With foreign withholding tax (most common for foreign securities)
        - "(Z)" = Withholding tax free
        - "(G)" = Withholding tax free capital gains
        - "KEP" = Capital contribution repayment (tax-free)
        - "(KG)" = Capital gain
        - "(KR)" = Return of capital
        """
        payment_list = []

        try:
            # Extract default currency from calc - used as fallback
            currency_raw = calc.get("currency", "CHF")
            if isinstance(currency_raw, dict):
                default_currency = currency_raw.get("shortName", "CHF")
            else:
                default_currency = str(currency_raw) if currency_raw else "CHF"

            payment_signs = []
            if calc_details:
                entries = calc_details.get("entries", [])
                for entry in entries:
                    sign_value = entry.get("sign")
                    if (
                        entry.get("type") == "PAYMENT"
                        and isinstance(sign_value, str)
                        and len(sign_value.strip()) >= 3
                    ):
                        payment_signs.append(sign_value.strip())

            payment_counter = 0

            # PRIMARY: Extract individual payment entries from calculationDetails.entries
            if calc_details:
                entries = calc_details.get("entries", [])
                payment_entries = [e for e in entries if e.get("type") == "PAYMENT"]

                if payment_entries:
                    for entry in payment_entries:
                        try:
                            # Extract payment date (milliseconds timestamp)
                            payment_date_ms = entry.get("paymentDate")
                            if payment_date_ms:
                                payment_date_obj = date.fromtimestamp(
                                    payment_date_ms / 1000
                                )
                            else:
                                # Fallback to year-end if no date
                                payment_date_obj = date(self.tax_year, 12, 31)

                            # Extract ex-date (milliseconds timestamp)
                            ex_date_ms = entry.get("exDate")
                            ex_date_obj = None
                            if ex_date_ms:
                                ex_date_obj = date.fromtimestamp(ex_date_ms / 1000)

                            # Extract value and valueChf
                            value = entry.get("value")
                            value_chf = entry.get("valueChf")

                            # Extract exchange rate
                            exchange_rate = entry.get("exchangeRate")
                            if exchange_rate is not None:
                                exchange_rate = Decimal(str(exchange_rate))

                            # Extract sign (tax treatment indicator)
                            sign_raw = entry.get("sign")
                            sign = (
                                sign_raw.strip()
                                if isinstance(sign_raw, str)
                                and len(sign_raw.strip()) >= 3
                                else None
                            )

                            # Extract currency - can be dict or string
                            entry_currency_raw = entry.get("currency", default_currency)
                            if isinstance(entry_currency_raw, dict):
                                currency = entry_currency_raw.get(
                                    "shortName", default_currency
                                )
                            else:
                                currency = (
                                    str(entry_currency_raw)
                                    if entry_currency_raw
                                    else default_currency
                                )

                            explicit_with_holding_tax = entry.get("withHoldingTax")
                            with_tax_total = entry.get(
                                "paymentValueTotalChfWithHoldingTax"
                            )
                            without_tax_total = entry.get(
                                "paymentValueTotalChfWithoutHoldingTax"
                            )

                            if explicit_with_holding_tax is not None:
                                with_holding_tax = bool(explicit_with_holding_tax)
                            elif (
                                with_tax_total is not None
                                or without_tax_total is not None
                            ):
                                with_holding_tax = Decimal(
                                    str(with_tax_total or 0)
                                ) > Decimal("0")
                            else:
                                with_holding_tax = (
                                    bool(sign)
                                    and infer_with_holding_from_sign
                                    and sign == "(Q)"
                                )

                            # Generate unique ID for this payment
                            payment_id = (
                                hash(
                                    f"payment_{self.tax_year}_{payment_counter}_"
                                    f"{payment_date_obj}_{value}"
                                )
                                & POSITIVE_INT_MASK
                            )
                            payment_counter += 1

                            payment_params = {
                                "id": payment_id,
                                "paymentDate": payment_date_obj,
                                "currency": currency,
                                "paymentValue": Decimal(str(value)) if value else None,
                                "paymentValueCHF": Decimal(str(value_chf)) if value_chf else None,
                                "exchangeRate": exchange_rate,
                                "withHoldingTax": with_holding_tax,
                                "sign": sign,
                                "exDate": ex_date_obj,
                            }
                            payment_entry = payment_model(**payment_params)
                            payment_list.append(payment_entry)

                        except Exception as e:
                            logger.warning(
                                f"Error processing payment entry: {e}", exc_info=True
                            )
                            continue

        except Exception as e:
            logger.warning(f"Error extracting payment data: {e}", exc_info=True)

        return payment_list

    @staticmethod
    def _date_to_ms(d: date) -> int:
        """Convert date to milliseconds since epoch."""
        dt = datetime.combine(d, datetime.min.time())
        return int(dt.timestamp() * 1000)

    @staticmethod
    def _normalize_security_name(name: Optional[str]) -> Optional[str]:
        """Normalize API security names to align with local kursliste naming."""
        if not name:
            return name
        if "," not in name:
            return name
        return name.split(",", 1)[0].strip()

    @staticmethod
    def _extract_name_parts(name: Optional[str]) -> list[str]:
        if not name:
            return []
        return [part.strip() for part in name.split(",") if part.strip()]

    @staticmethod
    def _infer_share_type(security_appendix: Optional[str]) -> SecurityTypeESTV:
        if not security_appendix:
            return SecurityTypeESTV.SHARE_COMMON
        appendix_lower = security_appendix.lower()
        if "stamm" in appendix_lower:
            return SecurityTypeESTV.SHARE_COMMON
        return SecurityTypeESTV.SHARE_NOMINAL

    @staticmethod
    def _infer_fund_type(name_parts: list[str]) -> SecurityTypeESTV:
        joined = " ".join(name_parts).lower()
        if "acc" in joined or "accumulation" in joined:
            return SecurityTypeESTV.FUND_ACCUMULATION
        return SecurityTypeESTV.FUND_DISTRIBUTION

    @staticmethod
    def _split_institution_name_and_appendix(
        raw_name: str,
    ) -> tuple[Optional[str], Optional[str]]:
        if not raw_name:
            return None, None
        marker = " iShares "
        idx = raw_name.find(marker)
        if idx > 0:
            return raw_name[:idx].strip(), raw_name[idx + 1 :].strip()
        return raw_name.strip(), None

    def get_securities_by_valor(self, valor_number: int) -> list:
        """
        Finds all securities by VALOR number.

        For the online accessor, this returns a list containing at most one security,
        since the API returns a single result per query.

        Args:
            valor_number: The VALOR number to search for.

        Returns:
            List containing the security if found, empty list otherwise.
        """
        security = self.get_security_by_valor(valor_number)
        return [security] if security else []

    def get_securities_by_isin(self, isin: str) -> list:
        """
        Finds all securities by ISIN.

        For the online accessor, this returns a list containing at most one security,
        since the API returns a single result per query.

        Args:
            isin: The ISIN to search for.

        Returns:
            List containing the security if found, empty list otherwise.
        """
        security = self.get_security_by_isin(isin)
        return [security] if security else []

    def get_sign_by_value(self, sign_value: str):
        """
        Retrieves a Sign object by its sign_value.

        The online API does not provide sign data, so this delegates to the
        fallback accessor if available.

        Args:
            sign_value: The sign value to look up.

        Returns:
            Sign object if found via fallback, None otherwise.
        """
        if self.fallback_accessor:
            return self.fallback_accessor.get_sign_by_value(sign_value)
        return None

    def get_da1_rate(
        self,
        country: str,
        security_group,
        security_type=None,
        da1_rate_type=None,
        reference_date: Optional[date] = None,
    ):
        """
        Retrieves a Da1Rate object based on criteria.

        The online API does not provide DA-1 rate data, so this delegates to the
        fallback accessor if available.

        Args:
            country: The country code.
            security_group: The SecurityGroupESTV enum value.
            security_type: Optional SecurityTypeESTV enum value.
            da1_rate_type: Optional Da1RateType enum value.
            reference_date: Optional reference date for validity filtering.

        Returns:
            Da1Rate object if found via fallback, None otherwise.
        """
        if self.fallback_accessor:
            return self.fallback_accessor.get_da1_rate(
                country, security_group, security_type, da1_rate_type, reference_date
            )
        return None
