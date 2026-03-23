document.addEventListener('DOMContentLoaded', () => {
    // ===== Mobile Navigation =====
    const navToggle = document.getElementById('nav-toggle');
    const navMenu = document.getElementById('nav-menu');
    const navLinks = document.querySelectorAll('.nav__link');

    navToggle.addEventListener('click', () => {
        const isOpen = navMenu.classList.toggle('open');
        navToggle.classList.toggle('active');
        navToggle.setAttribute('aria-expanded', isOpen);
        document.body.classList.toggle('no-scroll', isOpen);
    });

    navLinks.forEach(link => {
        link.addEventListener('click', () => {
            navMenu.classList.remove('open');
            navToggle.classList.remove('active');
            navToggle.setAttribute('aria-expanded', 'false');
            document.body.classList.remove('no-scroll');
        });
    });

    // ===== Header Scroll Effect =====
    const header = document.getElementById('header');

    window.addEventListener('scroll', () => {
        header.classList.toggle('scrolled', window.scrollY > 50);
    });

    // ===== Active Nav Highlighting =====
    const sections = document.querySelectorAll('section[id]');

    const navObserver = new IntersectionObserver(entries => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                navLinks.forEach(link => {
                    link.classList.toggle(
                        'active',
                        link.getAttribute('href') === `#${entry.target.id}`
                    );
                });
            }
        });
    }, { threshold: 0.3, rootMargin: '-80px 0px 0px 0px' });

    sections.forEach(section => navObserver.observe(section));

    // ===== Portfolio Filter =====
    const filterBtns = document.querySelectorAll('.filter-btn');
    const portfolioItems = document.querySelectorAll('.portfolio__item');

    filterBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            filterBtns.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');

            const filter = btn.dataset.filter;

            portfolioItems.forEach(item => {
                const match = filter === 'all' || item.dataset.category === filter;
                item.classList.toggle('hidden', !match);
            });
        });
    });

    // ===== Lightbox =====
    const lightbox = document.getElementById('lightbox');
    const lightboxImage = document.getElementById('lightbox-image');
    const lightboxTitle = document.getElementById('lightbox-title');
    const lightboxDesc = document.getElementById('lightbox-desc');
    let currentLightboxIndex = 0;
    let visibleItems = [];

    function getVisibleItems() {
        return Array.from(portfolioItems).filter(
            item => !item.classList.contains('hidden')
        );
    }

    function openLightbox(index) {
        visibleItems = getVisibleItems();
        currentLightboxIndex = index;
        updateLightbox();
        lightbox.classList.add('active');
        document.body.classList.add('no-scroll');
    }

    function closeLightbox() {
        lightbox.classList.remove('active');
        document.body.classList.remove('no-scroll');
    }

    function updateLightbox() {
        const item = visibleItems[currentLightboxIndex];
        const thumb = item.querySelector('.portfolio__thumb');
        const data = item.querySelector('.portfolio__data');

        lightboxImage.style.backgroundColor = thumb.style.backgroundColor;
        lightboxTitle.textContent = data.dataset.title;
        lightboxDesc.textContent = data.dataset.description;
    }

    function navigateLightbox(direction) {
        currentLightboxIndex = (currentLightboxIndex + direction + visibleItems.length) % visibleItems.length;
        updateLightbox();
    }

    portfolioItems.forEach(item => {
        item.addEventListener('click', () => {
            const visItems = getVisibleItems();
            const index = visItems.indexOf(item);
            if (index !== -1) openLightbox(index);
        });
    });

    lightbox.querySelector('.lightbox__close').addEventListener('click', closeLightbox);
    lightbox.querySelector('.lightbox__prev').addEventListener('click', () => navigateLightbox(-1));
    lightbox.querySelector('.lightbox__next').addEventListener('click', () => navigateLightbox(1));

    lightbox.addEventListener('click', e => {
        if (e.target === lightbox) closeLightbox();
    });

    document.addEventListener('keydown', e => {
        if (!lightbox.classList.contains('active')) return;
        if (e.key === 'Escape') closeLightbox();
        if (e.key === 'ArrowLeft') navigateLightbox(-1);
        if (e.key === 'ArrowRight') navigateLightbox(1);
    });

    // ===== Scroll Animations =====
    const animateElements = document.querySelectorAll('.animate-on-scroll');

    const scrollObserver = new IntersectionObserver(entries => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                entry.target.classList.add('visible');
                scrollObserver.unobserve(entry.target);
            }
        });
    }, { threshold: 0.1 });

    animateElements.forEach(el => scrollObserver.observe(el));

    // ===== Back to Top =====
    const backToTop = document.getElementById('back-to-top');

    window.addEventListener('scroll', () => {
        backToTop.classList.toggle('visible', window.scrollY > 600);
    });

    backToTop.addEventListener('click', () => {
        window.scrollTo({ top: 0, behavior: 'smooth' });
    });

    // ===== Contact Form Validation =====
    const form = document.getElementById('contact-form');
    const formStatus = document.getElementById('form-status');

    function validateEmail(email) {
        return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
    }

    function showError(input, message) {
        const group = input.closest('.form-group');
        group.classList.add('error');
        group.querySelector('.form-error').textContent = message;
    }

    function clearErrors() {
        form.querySelectorAll('.form-group').forEach(group => {
            group.classList.remove('error');
        });
        formStatus.textContent = '';
        formStatus.className = 'form-status';
    }

    form.addEventListener('submit', e => {
        e.preventDefault();
        clearErrors();

        const name = form.querySelector('#name');
        const email = form.querySelector('#email');
        const message = form.querySelector('#message');
        let valid = true;

        if (!name.value.trim()) {
            showError(name, 'Please enter your name');
            valid = false;
        }

        if (!email.value.trim()) {
            showError(email, 'Please enter your email');
            valid = false;
        } else if (!validateEmail(email.value.trim())) {
            showError(email, 'Please enter a valid email');
            valid = false;
        }

        if (!message.value.trim()) {
            showError(message, 'Please enter a message');
            valid = false;
        }

        if (!valid) return;

        // Submit to Formspree
        const formData = new FormData(form);

        fetch(form.action, {
            method: 'POST',
            body: formData,
            headers: { 'Accept': 'application/json' }
        })
        .then(response => {
            if (response.ok) {
                formStatus.textContent = 'Message sent successfully!';
                formStatus.className = 'form-status success';
                form.reset();
            } else {
                formStatus.textContent = 'Something went wrong. Please try again.';
                formStatus.className = 'form-status error';
            }
        })
        .catch(() => {
            formStatus.textContent = 'Network error. Please try again later.';
            formStatus.className = 'form-status error';
        });
    });
});
